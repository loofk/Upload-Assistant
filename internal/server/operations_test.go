package server

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

func TestOperationalLogCursorRoundTripAndLegacyCompatibility(t *testing.T) {
	occurredAt := time.Date(2026, 8, 10, 7, 14, 43, 123, time.UTC)
	cursor := encodeOperationalLogCursor(occurredAt, 42)
	decodedAt, decodedID, err := decodeOperationalLogCursor(cursor)
	if err != nil || decodedAt == nil || !decodedAt.Equal(occurredAt) || decodedID != 42 {
		t.Fatalf("decoded log cursor = %v/%d err=%v", decodedAt, decodedID, err)
	}
	legacy := base64.RawURLEncoding.EncodeToString([]byte("41"))
	decodedAt, decodedID, err = decodeOperationalLogCursor(legacy)
	if err != nil || decodedAt != nil || decodedID != 41 {
		t.Fatalf("decoded legacy log cursor = %v/%d err=%v", decodedAt, decodedID, err)
	}
	if _, _, err = decodeOperationalLogCursor("not-base64!"); err == nil {
		t.Fatal("malformed log cursor was accepted")
	}
}

func TestRuleAnalysisCoordinatorSharesOneProviderCallAndReplaysResult(t *testing.T) {
	coordinator := newRuleAnalysisCoordinator(time.Minute, 8)
	started := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	run := func() ruleAnalysisOutcome {
		if calls.Add(1) == 1 {
			close(started)
		}
		<-release
		return ruleAnalysisOutcome{}
	}
	type response struct {
		replayed bool
		err      error
	}
	responses := make(chan response, 2)
	var wait sync.WaitGroup
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			_, replayed, err := coordinator.Do(context.Background(), "user:click", "same-input", run)
			responses <- response{replayed: replayed, err: err}
		}()
		<-started
	}
	close(release)
	wait.Wait()
	close(responses)
	if calls.Load() != 1 {
		t.Fatalf("provider calls = %d, want 1", calls.Load())
	}
	replayedCount := 0
	for item := range responses {
		if item.err != nil {
			t.Fatal(item.err)
		}
		if item.replayed {
			replayedCount++
		}
	}
	if replayedCount != 1 {
		t.Fatalf("replayed responses = %d, want 1", replayedCount)
	}
	_, replayed, err := coordinator.Do(context.Background(), "user:click", "same-input", func() ruleAnalysisOutcome {
		t.Fatal("completed replay invoked provider")
		return ruleAnalysisOutcome{}
	})
	if err != nil || !replayed {
		t.Fatalf("completed replay = %v, err=%v", replayed, err)
	}
}

func TestRuleAnalysisCoordinatorRejectsKeyReuseWithDifferentInput(t *testing.T) {
	coordinator := newRuleAnalysisCoordinator(time.Minute, 8)
	_, _, err := coordinator.Do(context.Background(), "user:click", "first", func() ruleAnalysisOutcome { return ruleAnalysisOutcome{} })
	if err != nil {
		t.Fatal(err)
	}
	_, replayed, err := coordinator.Do(context.Background(), "user:click", "second", func() ruleAnalysisOutcome {
		t.Fatal("conflicting replay invoked provider")
		return ruleAnalysisOutcome{}
	})
	if !replayed || !errors.Is(err, errRuleAnalysisIdempotencyConflict) {
		t.Fatalf("conflicting replay = %v, err=%v", replayed, err)
	}
}

func TestRuleAnalysisCoordinatorRejectsNewWorkWhenCapacityIsRunning(t *testing.T) {
	coordinator := newRuleAnalysisCoordinator(time.Minute, 1)
	started := make(chan struct{})
	release := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, _, err := coordinator.Do(context.Background(), "first", "input", func() ruleAnalysisOutcome {
			close(started)
			<-release
			return ruleAnalysisOutcome{}
		})
		done <- err
	}()
	<-started
	_, replayed, err := coordinator.Do(context.Background(), "second", "input", func() ruleAnalysisOutcome {
		t.Fatal("capacity-rejected work invoked provider")
		return ruleAnalysisOutcome{}
	})
	if replayed || !errors.Is(err, errRuleAnalysisCapacity) {
		t.Fatalf("capacity result replayed/error = %t/%v", replayed, err)
	}
	close(release)
	if err = <-done; err != nil {
		t.Fatal(err)
	}
}

func TestProviderFailureHTTPStatus(t *testing.T) {
	if providerFailureHTTPStatus("provider_timeout") != http.StatusGatewayTimeout || providerFailureHTTPStatus("provider_busy") != http.StatusServiceUnavailable || providerFailureHTTPStatus("provider_configuration_changed") != http.StatusConflict || providerFailureHTTPStatus("provider_http_error") != http.StatusBadGateway {
		t.Fatal("provider failure HTTP status mapping changed")
	}
}

func TestGetSiteRuleAnalysisResultReturnsCompletedIdempotentOutcome(t *testing.T) {
	coordinator := newRuleAnalysisCoordinator(time.Minute, 8)
	principal := security.Principal{UserID: "operator", Role: "admin", TokenScopes: []string{"config:manage"}}
	key := ruleAnalysisCacheKey(principal, "browser-click")
	_, _, err := coordinator.Do(context.Background(), key, "input", func() ruleAnalysisOutcome {
		return ruleAnalysisOutcome{Result: operations.RuleAnalysisResult{DraftMarkdown: "draft", SourceComplete: true}}
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/site-rules/analyze/result", nil)
	request.Header.Set("Idempotency-Key", "browser-click")
	request = request.WithContext(security.WithPrincipal(request.Context(), principal))
	response := httptest.NewRecorder()
	operationsAPI{ruleAnalyses: coordinator}.getSiteRuleAnalysisResult(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("result status = %d body=%s", response.Code, response.Body.String())
	}
	var envelope struct {
		Status   string                        `json:"status"`
		Analysis operations.RuleAnalysisResult `json:"analysis"`
	}
	if err = json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || envelope.Status != "draft_ready" || envelope.Analysis.DraftMarkdown != "draft" {
		t.Fatalf("result envelope = %#v err=%v", envelope, err)
	}
}
