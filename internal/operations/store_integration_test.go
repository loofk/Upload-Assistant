package operations

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

func TestOperationalStoreLifecycle(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err = database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	store := NewStore(pool)
	userID := uuid.NewString()
	username := "ops-" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	_, err = pool.Exec(ctx, `INSERT INTO users(id,username,password_hash,role) VALUES($1,$2,'fixture','admin')`, userID, username)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), `DELETE FROM users WHERE id=$1`, userID) })
	traceID := uuid.NewString()
	var errorLogID int64
	for index, level := range []string{"info", "warn", "error"} {
		logID, insertErr := store.InsertLog(ctx, LogEntry{Level: level, Component: "integration", Message: "bounded entry", TraceID: traceID, ErrorCode: func() string {
			if index == 2 {
				return "fixture_failed"
			}
			return ""
		}(), Attributes: json.RawMessage(`{"token":"must-not-persist","index":1,"action":"fixture_action","error_detail":"fixture detail"}`)})
		if insertErr != nil {
			t.Fatal(insertErr)
		}
		if index == 2 {
			errorLogID = logID
		}
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM operational_logs WHERE trace_id=$1`, traceID)
	})
	page, err := store.ListLogs(ctx, LogFilter{TraceID: traceID, Levels: []string{"error"}, Limit: 10})
	if err != nil || len(page.Logs) != 1 || strings.Contains(string(page.Logs[0].Attributes), "must-not-persist") {
		t.Fatalf("filtered/redacted logs=%#v err=%v", page, err)
	}
	summaries, err := store.ListLogSummaries(ctx, LogFilter{TraceID: traceID, Keyword: "fixture detail", Limit: 10})
	if err != nil || len(summaries.Logs) != 3 || summaries.Logs[0].Attributes != nil || summaries.Logs[0].Action != "fixture_action" || summaries.Logs[0].ErrorDetail != "fixture detail" {
		t.Fatalf("lightweight log summaries=%#v err=%v", summaries, err)
	}
	incident, err := store.UpsertIncident(ctx, IncidentInput{Severity: "warning", Kind: "fixture", Fingerprint: "fixture-" + uuid.NewString(), Title: "Fixture incident", Summary: "first", TraceID: traceID, Evidence: map[string]any{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), `DELETE FROM incidents WHERE id=$1`, incident.ID) })
	incident, err = store.UpsertIncident(ctx, IncidentInput{Severity: "critical", Kind: "fixture", Fingerprint: incident.Fingerprint, Title: "Fixture incident", Summary: "second", TraceID: traceID})
	if err != nil || incident.OccurrenceCount != 2 {
		t.Fatalf("merged incident=%#v err=%v", incident, err)
	}
	concurrentFingerprint := "concurrent-" + uuid.NewString()
	var group sync.WaitGroup
	for index := 0; index < 8; index++ {
		group.Add(1)
		go func() {
			defer group.Done()
			_, _ = store.UpsertIncident(context.Background(), IncidentInput{Severity: "warning", Kind: "fixture", Fingerprint: concurrentFingerprint, Title: "Concurrent incident", Summary: "same fingerprint"})
		}()
	}
	group.Wait()
	concurrent, err := store.ListIncidents(ctx, IncidentFilter{Kind: "fixture", Limit: 20})
	if err != nil {
		t.Fatal(err)
	}
	foundConcurrent := false
	for _, item := range concurrent.Incidents {
		if item.Fingerprint == concurrentFingerprint {
			foundConcurrent = item.OccurrenceCount == 8
			t.Cleanup(func() { _, _ = pool.Exec(context.Background(), `DELETE FROM incidents WHERE id=$1`, item.ID) })
		}
	}
	if !foundConcurrent {
		t.Fatalf("concurrent fingerprint did not aggregate exactly: %#v", concurrent.Incidents)
	}
	principal := security.Principal{UserID: userID, Username: username, Role: "admin", TokenID: uuid.NewString(), TokenScopes: []string{"*"}}
	incident, err = store.SetIncidentStatus(ctx, incident.ID, "acknowledged", principal, traceID)
	if err != nil || incident.Status != "acknowledged" {
		t.Fatalf("ack incident=%#v err=%v", incident, err)
	}
	logContext, err := store.GetLogContext(ctx, errorLogID)
	if err != nil || logContext.Log.ErrorCode != "fixture_failed" || len(logContext.CorrelatedLogs) != 3 || len(logContext.AuditEvents) == 0 || logContext.AuditEvents[0].Action != "incident.acknowledged" {
		t.Fatalf("log context=%#v err=%v", logContext, err)
	}
	providerService := &DiagnosticService{Store: store, Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 524, Header: http.Header{"Content-Type": []string{"text/plain"}}, Body: io.NopCloser(strings.NewReader("origin timed out api_key=provider-secret"))}, nil
	})}}
	providerContext := WithCorrelation(ctx, Correlation{RequestID: "provider-request-fixture", TraceID: traceID})
	_, _, _, providerErr := providerService.doProviderCompletion(providerContext, Provider{ID: uuid.NewString(), BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "fixture-model", APIMode: ProviderAPIModeChatCompletions, ReasoningEffort: "high", TimeoutSeconds: 60}, "fixture_external_call", "system", "token=request-secret", true)
	if providerErr == nil || providerCallErrorCode(providerErr) != "provider_http_error" {
		t.Fatalf("provider completion error = %v", providerErr)
	}
	providerLogs, err := store.ListLogs(ctx, LogFilter{TraceID: traceID, Component: "external.llm", Limit: 10})
	if err != nil || len(providerLogs.Logs) != 1 {
		t.Fatalf("external provider logs=%#v err=%v", providerLogs, err)
	}
	providerLogBody := string(providerLogs.Logs[0].Attributes)
	if providerLogs.Logs[0].StatusCode != 524 || providerLogs.Logs[0].ErrorCode != "provider_http_error" || !strings.Contains(providerLogBody, "fixture_external_call") || !strings.Contains(providerLogBody, "request_metadata") || !strings.Contains(providerLogBody, "response_metadata") || !strings.Contains(providerLogBody, "content_bytes") || strings.Contains(providerLogBody, "origin timed out") || strings.Contains(providerLogBody, "provider-secret") || strings.Contains(providerLogBody, "request-secret") {
		t.Fatalf("external provider log = %#v", providerLogs.Logs[0])
	}
	settings, err := store.GetSettings(ctx)
	if err != nil {
		t.Fatal(err)
	}
	filesystemRoot := t.TempDir()
	overview, err := store.Overview(ctx, filesystemRoot, filesystemRoot, filesystemRoot, "integration-test", 3)
	if err != nil {
		t.Fatalf("operations overview: %v", err)
	}
	if len(overview.Filesystems) != 3 || overview.ApplicationVersion != "integration-test" || overview.LogSinkDropped != 3 {
		t.Fatalf("unexpected operations overview: %#v", overview)
	}
	originalSettings := settings
	t.Cleanup(func() { _, _ = store.PutSettings(context.Background(), originalSettings, principal, traceID) })
	settings.QueueWarningCount = 21
	if _, err = store.PutSettings(ctx, settings, principal, traceID); err != nil {
		t.Fatal(err)
	}
	capacityKey := "fixture-capacity-" + uuid.NewString()
	if err = store.evaluateMeasurement(ctx, capacityKey, 80, 80, 90, settings, "fixture threshold"); err != nil {
		t.Fatal(err)
	}
	var firstNotified, secondNotified time.Time
	if err = pool.QueryRow(ctx, `SELECT last_notified_at FROM capacity_alert_state WHERE fingerprint=$1`, capacityKey).Scan(&firstNotified); err != nil {
		t.Fatal(err)
	}
	if err = store.evaluateMeasurement(ctx, capacityKey, 81, 80, 90, settings, "fixture threshold"); err != nil {
		t.Fatal(err)
	}
	if err = pool.QueryRow(ctx, `SELECT last_notified_at FROM capacity_alert_state WHERE fingerprint=$1`, capacityKey).Scan(&secondNotified); err != nil || !firstNotified.Equal(secondNotified) {
		t.Fatalf("capacity cooldown timestamp = %v/%v, err=%v", firstNotified, secondNotified, err)
	}
	digest := sha256.Sum256([]byte(capacityKey))
	capacityFingerprint := hex.EncodeToString(digest[:])
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM capacity_alert_state WHERE fingerprint=$1`, capacityKey)
		_, _ = pool.Exec(context.Background(), `DELETE FROM incidents WHERE fingerprint=$1`, capacityFingerprint)
	})
	if err = store.evaluateMeasurement(ctx, capacityKey, 74, 80, 90, settings, "fixture recovered"); err != nil {
		t.Fatal(err)
	}
	var capacityIncidentStatus string
	if err = pool.QueryRow(ctx, `SELECT status FROM incidents WHERE fingerprint=$1`, capacityFingerprint).Scan(&capacityIncidentStatus); err != nil || capacityIncidentStatus != "resolved" {
		t.Fatalf("capacity incident recovery = %q, err=%v", capacityIncidentStatus, err)
	}
	healthResource := "health-" + uuid.NewString()
	for index := 0; index < 2; index++ {
		if _, err = pool.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,payload) VALUES('test','fixture','downloader.health','downloader',$1,'{"status":"failed"}')`, healthResource); err != nil {
			t.Fatal(err)
		}
	}
	var healthIncidentID string
	if err = pool.QueryRow(ctx, `SELECT id::text FROM incidents WHERE kind='integration_health' AND evidence->>'resource_id'=$1`, healthResource).Scan(&healthIncidentID); err != nil {
		t.Fatalf("consecutive health failures did not create an incident: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM incidents WHERE id=$1`, healthIncidentID)
		_, _ = pool.Exec(context.Background(), `DELETE FROM audit_events WHERE resource_id=$1`, healthResource)
	})
	auth := security.NewAuthStore(pool)
	created, err := auth.CreateToken(ctx, principal, security.CreateTokenInput{Name: "ops-fixture", Scopes: []string{"logs:read", "operations:read"}}, traceID)
	if err != nil || created.Token == "" {
		t.Fatalf("CreateToken=%#v err=%v", created, err)
	}
	authenticated, err := auth.AuthenticateToken(ctx, created.Token)
	if err != nil || !authenticated.HasScope("logs:read") || authenticated.HasScope("logs:export") {
		t.Fatalf("authenticated=%#v err=%v", authenticated, err)
	}
	if _, err = auth.RevokeToken(ctx, principal, created.ID, traceID); err != nil {
		t.Fatal(err)
	}
	if _, err = auth.AuthenticateToken(ctx, created.Token); err == nil {
		t.Fatal("revoked token authenticated")
	}
}
