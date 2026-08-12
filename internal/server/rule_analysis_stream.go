package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

const ruleAnalysisHeartbeatInterval = 10 * time.Second

var errRuleAnalysisIdempotencyConflict = errors.New("rule analysis idempotency key was reused with different input")
var errRuleAnalysisCapacity = errors.New("rule analysis capacity is full")

type ruleAnalysisStreamInput struct {
	ProviderID       string   `json:"provider_id"`
	SourceRevisionID string   `json:"source_revision_id"`
	SiteCode         string   `json:"site_code"`
	DisplayName      string   `json:"display_name"`
	Roles            []string `json:"roles"`
	SourceURL        string   `json:"source_url"`
	SourceScope      string   `json:"source_scope"`
	SourceComplete   bool     `json:"source_complete"`
	SourceText       string   `json:"source_text"`
}

func (input ruleAnalysisStreamInput) operationInput() operations.RuleAnalysisInput {
	return operations.RuleAnalysisInput{
		ProviderID: input.ProviderID, SourceRevisionID: input.SourceRevisionID,
		SiteCode: input.SiteCode, DisplayName: input.DisplayName, Roles: input.Roles,
		SourceURL: input.SourceURL, SourceScope: input.SourceScope,
		SourceComplete: input.SourceComplete, SourceText: input.SourceText,
	}
}

type ruleAnalysisOutcome struct {
	Result operations.RuleAnalysisResult
	Err    error
}

type ruleAnalysisCacheEntry struct {
	inputHash   string
	done        chan struct{}
	outcome     ruleAnalysisOutcome
	completedAt time.Time
}

// ruleAnalysisCoordinator prevents a reverse proxy from turning one explicit
// operator action into multiple provider calls. Completed results are retained
// only in bounded process memory for a short replay window.
type ruleAnalysisCoordinator struct {
	mu      sync.Mutex
	entries map[string]*ruleAnalysisCacheEntry
	ttl     time.Duration
	max     int
}

func newRuleAnalysisCoordinator(ttl time.Duration, max int) *ruleAnalysisCoordinator {
	return &ruleAnalysisCoordinator{entries: make(map[string]*ruleAnalysisCacheEntry), ttl: ttl, max: max}
}

func (coordinator *ruleAnalysisCoordinator) Do(ctx context.Context, key, inputHash string, run func() ruleAnalysisOutcome) (ruleAnalysisOutcome, bool, error) {
	now := time.Now()
	coordinator.mu.Lock()
	coordinator.pruneLocked(now)
	if existing, ok := coordinator.entries[key]; ok {
		if existing.inputHash != inputHash {
			coordinator.mu.Unlock()
			return ruleAnalysisOutcome{}, true, errRuleAnalysisIdempotencyConflict
		}
		done := existing.done
		coordinator.mu.Unlock()
		select {
		case <-ctx.Done():
			return ruleAnalysisOutcome{}, true, ctx.Err()
		case <-done:
			return existing.outcome, true, nil
		}
	}
	if coordinator.max > 0 && len(coordinator.entries) >= coordinator.max {
		oldestKey := ""
		var oldest time.Time
		for candidateKey, candidate := range coordinator.entries {
			if candidate.completedAt.IsZero() {
				continue
			}
			if oldestKey == "" || candidate.completedAt.Before(oldest) {
				oldestKey, oldest = candidateKey, candidate.completedAt
			}
		}
		if oldestKey == "" {
			coordinator.mu.Unlock()
			return ruleAnalysisOutcome{}, false, errRuleAnalysisCapacity
		}
		delete(coordinator.entries, oldestKey)
	}
	entry := &ruleAnalysisCacheEntry{inputHash: inputHash, done: make(chan struct{})}
	coordinator.entries[key] = entry
	coordinator.mu.Unlock()

	outcome := run()
	coordinator.mu.Lock()
	entry.outcome = outcome
	entry.completedAt = time.Now()
	close(entry.done)
	coordinator.pruneLocked(entry.completedAt)
	coordinator.mu.Unlock()
	return outcome, false, nil
}

func (coordinator *ruleAnalysisCoordinator) Get(key string) (ruleAnalysisOutcome, bool, bool) {
	coordinator.mu.Lock()
	defer coordinator.mu.Unlock()
	coordinator.pruneLocked(time.Now())
	entry, found := coordinator.entries[key]
	if !found {
		return ruleAnalysisOutcome{}, false, false
	}
	if entry.completedAt.IsZero() {
		return ruleAnalysisOutcome{}, false, true
	}
	return entry.outcome, true, true
}

func (coordinator *ruleAnalysisCoordinator) pruneLocked(now time.Time) {
	for key, entry := range coordinator.entries {
		if !entry.completedAt.IsZero() && now.Sub(entry.completedAt) > coordinator.ttl {
			delete(coordinator.entries, key)
		}
	}
	for len(coordinator.entries) > coordinator.max {
		oldestKey := ""
		var oldest time.Time
		for key, entry := range coordinator.entries {
			if entry.completedAt.IsZero() {
				continue
			}
			if oldestKey == "" || entry.completedAt.Before(oldest) {
				oldestKey, oldest = key, entry.completedAt
			}
		}
		if oldestKey == "" {
			return
		}
		delete(coordinator.entries, oldestKey)
	}
}

func (api operationsAPI) analyzeSiteRulesStream(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" || len(idempotencyKey) > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_idempotency_key", "Idempotency-Key is required and must not exceed 200 characters")
		return
	}
	var input ruleAnalysisStreamInput
	if err := decodeJSONLimit(w, r, &input, 2*operations.MaxRuleAnalysisBytes+(1<<20)); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeProblem(w, http.StatusInternalServerError, "stream_unavailable", "SSE is unavailable")
		return
	}
	body, _ := json.Marshal(input)
	digest := sha256.Sum256(body)
	inputHash := hex.EncodeToString(digest[:])
	cacheKey := ruleAnalysisCacheKey(principal, idempotencyKey)
	coordinator := api.ruleAnalyses
	if coordinator == nil {
		coordinator = newRuleAnalysisCoordinator(10*time.Minute, 16)
	}

	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	startedAt := time.Now()
	if writeRuleAnalysisEvent(w, "analysis-started", map[string]any{"status": "analyzing"}) != nil {
		return
	}
	flusher.Flush()

	resultChannel := make(chan struct {
		outcome  ruleAnalysisOutcome
		replayed bool
		err      error
	}, 1)
	operationContext := context.WithoutCancel(r.Context())
	go func() {
		outcome, replayed, err := coordinator.Do(operationContext, cacheKey, inputHash, func() ruleAnalysisOutcome {
			result, runErr := api.diagnostics.AnalyzeRuleText(operationContext, input.operationInput(), principal, operations.CorrelationFromContext(operationContext).TraceID)
			return ruleAnalysisOutcome{Result: result, Err: runErr}
		})
		resultChannel <- struct {
			outcome  ruleAnalysisOutcome
			replayed bool
			err      error
		}{outcome: outcome, replayed: replayed, err: err}
	}()

	heartbeat := time.NewTicker(ruleAnalysisHeartbeatInterval)
	defer heartbeat.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.C:
			if writeRuleAnalysisEvent(w, "analysis-progress", map[string]any{"status": "analyzing", "elapsed_seconds": int(time.Since(startedAt).Seconds())}) != nil {
				return
			}
			flusher.Flush()
		case completed := <-resultChannel:
			if completed.err != nil {
				problem := describeRuleAnalysisProblem(completed.err)
				markStreamError(w, problem)
				_ = writeRuleAnalysisEvent(w, "analysis-error", problem.envelope())
				flusher.Flush()
				return
			}
			if completed.outcome.Err != nil {
				problem := describeRuleAnalysisProblem(completed.outcome.Err)
				markStreamError(w, problem)
				_ = writeRuleAnalysisEvent(w, "analysis-error", problem.envelope())
				flusher.Flush()
				return
			}
			_ = writeRuleAnalysisEvent(w, "analysis-result", ruleAnalysisSuccessEnvelope(completed.outcome.Result, completed.replayed))
			flusher.Flush()
			return
		}
	}
}

func (api operationsAPI) getSiteRuleAnalysisResult(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" || len(idempotencyKey) > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_idempotency_key", "Idempotency-Key is required and must not exceed 200 characters")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	if api.ruleAnalyses == nil {
		writeProblem(w, http.StatusNotFound, "analysis_request_not_found", "rule analysis request was not found or has expired")
		return
	}
	outcome, completed, found := api.ruleAnalyses.Get(ruleAnalysisCacheKey(principal, idempotencyKey))
	if !found {
		writeProblem(w, http.StatusNotFound, "analysis_request_not_found", "rule analysis request was not found or has expired")
		return
	}
	if !completed {
		writeJSON(w, http.StatusAccepted, map[string]any{
			"ok": true, "status": "analyzing", "blockers": []any{},
			"next_actions": []map[string]string{{"action": "poll_rule_analysis_result"}},
		})
		return
	}
	if outcome.Err != nil {
		problem := describeRuleAnalysisProblem(outcome.Err)
		writeProblemWithAttributes(w, problem.Status, problem.Code, problem.Detail, problem.Attributes, problem.NextActions)
		return
	}
	writeJSON(w, http.StatusOK, ruleAnalysisSuccessEnvelope(outcome.Result, true))
}

func ruleAnalysisCacheKey(principal security.Principal, idempotencyKey string) string {
	actorKey := principal.UserID
	if actorKey == "" {
		actorKey = principal.TokenID
	}
	return actorKey + ":" + idempotencyKey
}

func writeRuleAnalysisEvent(w http.ResponseWriter, event string, value any) error {
	body, err := json.Marshal(operations.Redact(value))
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, body)
	return err
}

type ruleAnalysisProblem struct {
	Status      int
	Code        string
	Detail      string
	Attributes  map[string]any
	NextActions []map[string]string
}

func (problem ruleAnalysisProblem) envelope() map[string]any {
	return map[string]any{
		"ok": false, "status": "failed", "http_status": problem.Status,
		"error":        map[string]string{"code": problem.Code, "detail": problem.Detail},
		"blockers":     []map[string]string{{"code": problem.Code, "message": problem.Detail}},
		"next_actions": problem.NextActions,
	}
}

func describeRuleAnalysisProblem(err error) ruleAnalysisProblem {
	switch {
	case errors.Is(err, errRuleAnalysisIdempotencyConflict):
		return ruleAnalysisProblem{Status: http.StatusConflict, Code: "idempotency_conflict", Detail: err.Error(), NextActions: []map[string]string{}}
	case errors.Is(err, errRuleAnalysisCapacity):
		return ruleAnalysisProblem{Status: http.StatusServiceUnavailable, Code: "provider_busy", Detail: "rule analysis capacity is full; retry after current analyses finish", NextActions: []map[string]string{{"action": "retry_later"}}}
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return ruleAnalysisProblem{Status: 499, Code: "request_cancelled", Detail: "rule analysis request was cancelled", NextActions: []map[string]string{}}
	case errors.Is(err, operations.ErrNotFound):
		return ruleAnalysisProblem{Status: http.StatusNotFound, Code: "not_found", Detail: "operations resource was not found", NextActions: []map[string]string{}}
	case errors.Is(err, operations.ErrConflict), errors.Is(err, security.ErrForbidden):
		return ruleAnalysisProblem{Status: http.StatusConflict, Code: "state_conflict", Detail: err.Error(), NextActions: []map[string]string{}}
	case errors.Is(err, operations.ErrInvalid):
		return ruleAnalysisProblem{Status: http.StatusBadRequest, Code: "invalid_request", Detail: err.Error(), NextActions: []map[string]string{}}
	}
	if failure, ok := operations.DescribeProviderCallFailure(err); ok {
		status := providerFailureHTTPStatus(failure.Code)
		return ruleAnalysisProblem{Status: status, Code: failure.Code, Detail: failure.Detail, NextActions: providerFailureNextActions(failure), Attributes: map[string]any{
			"operation": "site_rule_analysis", "provider_id": failure.Evidence.ProviderID,
			"model": failure.Evidence.Model, "api_mode": failure.Evidence.APIMode,
			"reasoning_effort": failure.Evidence.ReasoningEffort,
			"endpoint_path":    failure.Evidence.EndpointPath, "timeout_seconds": failure.Evidence.TimeoutSeconds,
			"upstream_status_code": failure.Evidence.StatusCode, "content_type": failure.Evidence.ContentType,
			"response_sha256": failure.Evidence.ResponseSHA256, "response_shape": failure.Evidence.ResponseShape,
			"stream_error": failure.Evidence.StreamError, "attempt": failure.Evidence.Attempt,
			"max_attempts": failure.Evidence.MaxAttempts, "provider_latency_ms": failure.Evidence.LatencyMS,
			"external_call_performed": failure.Evidence.ExternalCallPerformed,
		}}
	}
	return ruleAnalysisProblem{Status: http.StatusBadGateway, Code: "rule_analysis_failed", Detail: "the provider could not produce a valid rule draft", NextActions: []map[string]string{}}
}

func markStreamError(w http.ResponseWriter, problem ruleAnalysisProblem) {
	if recorder, ok := w.(interface{ SetErrorCode(string) }); ok {
		recorder.SetErrorCode(problem.Code)
	}
	if recorder, ok := w.(interface{ SetErrorDetail(string) }); ok {
		recorder.SetErrorDetail(problem.Detail)
	}
	if len(problem.Attributes) > 0 {
		if recorder, ok := w.(interface{ SetErrorAttributes(map[string]any) }); ok {
			recorder.SetErrorAttributes(problem.Attributes)
		}
	}
}

func ruleAnalysisSuccessEnvelope(result operations.RuleAnalysisResult, replayed bool) map[string]any {
	blockers := []any{}
	if !result.SourceComplete {
		blockers = append(blockers, map[string]any{"code": "rule_source_incomplete", "message": "原文尚未标记为完整，草稿不可审批或激活"})
	}
	return map[string]any{
		"ok": true, "status": "draft_ready", "analysis": result, "replayed": replayed,
		"blockers":     blockers,
		"next_actions": []map[string]any{{"action": "review_ai_rule_draft"}, {"action": "import_site_rule_markdown"}},
	}
}
