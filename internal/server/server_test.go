package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

type fakeDatabase struct{ err error }

func (f fakeDatabase) Ping(context.Context) error { return f.err }

type fakeAuthenticator struct {
	principal security.Principal
	err       error
}

func (f fakeAuthenticator) AuthenticateToken(context.Context, string) (security.Principal, error) {
	return f.principal, f.err
}

func TestLivenessDoesNotDependOnDatabase(t *testing.T) {
	handler := New(Dependencies{
		Database: fakeDatabase{err: errors.New("down")},
		DataDir:  t.TempDir(),
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
		Build:    buildinfo.Info{Version: "test"},
	})
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health/live", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
}

func TestEmbeddedWebUIIsPublicAndUsesStrictSecurityHeaders(t *testing.T) {
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(),
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
	})
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/", nil))
	if response.Code != http.StatusOK || response.Header().Get("Content-Type") != "text/html; charset=utf-8" {
		t.Fatalf("Web UI response = %d/%s", response.Code, response.Header().Get("Content-Type"))
	}
	if response.Header().Get("X-Content-Type-Options") != "nosniff" || response.Header().Get("X-Frame-Options") != "DENY" ||
		!strings.Contains(response.Header().Get("Content-Security-Policy"), "script-src 'self'") {
		t.Fatalf("Web UI security headers = %#v", response.Header())
	}
}

func TestOpenAPIAndToolContracts(t *testing.T) {
	var document struct {
		OpenAPI    string                    `json:"openapi"`
		Paths      map[string]map[string]any `json:"paths"`
		Components struct {
			Schemas map[string]json.RawMessage `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(openAPIDocument, &document); err != nil {
		t.Fatalf("embedded OpenAPI is invalid JSON: %v", err)
	}
	if document.OpenAPI != "3.1.0" {
		t.Fatalf("openapi = %s", document.OpenAPI)
	}
	requiredPaths := []string{
		"/.well-known/upload-assistant.json", "/.well-known/upload-assistant/SKILL.md",
		"/openapi.json", "/api/v2/tools", "/api/v2/jobs", "/api/v2/candidates/daily",
		"/api/v2/readiness/live",
		"/api/v2/candidates/{candidate_id}/retorrent-job",
		"/api/v2/jobs/{job_id}", "/api/v2/jobs/{job_id}/summary",
		"/api/v2/jobs/{job_id}/attention", "/api/v2/jobs/{job_id}/actions",
		"/api/v2/jobs/{job_id}/upload-preview", "/api/v2/jobs/{job_id}/upload-preview/revisions",
		"/api/v2/jobs/{job_id}/attempts",
		"/api/v2/jobs/{job_id}/artifacts/{artifact_id}/content",
		"/api/v2/jobs/{job_id}/resume", "/api/v2/jobs/{job_id}/replay", "/api/v2/sites/{site_code}/rules/active",
		"/api/v2/sites/{site_code}", "/api/v2/site-rules/{revision_id}/approve",
		"/api/v2/site-rules/{revision_id}/discard",
		"/api/v2/sites/{site_code}/access-policy",
		"/api/v2/sites/{site_code}/rule-sources", "/api/v2/sites/{site_code}/rule-collection-runs", "/api/v2/sites/{site_code}/rule-collection-runs/latest",
		"/api/v2/site-rule-collection-runs/{run_id}", "/api/v2/site-rule-collection-runs/{run_id}/stream",
		"/api/v2/site-rules/{revision_id}/review", "/api/v2/site-rules/{revision_id}/review/{section}",
		"/api/v2/site-rules/{revision_id}/corrections/{section}",
		"/api/v2/schedules/daily-candidates", "/api/v2/schedules/daily-candidates/{schedule_id}",
		"/api/v2/schedules/daily-candidates/{schedule_id}/runs",
		"/api/v2/notifications",
		"/api/v2/audit-events",
		"/api/v2/notification-channels", "/api/v2/notification-channels/{name}",
		"/api/v2/notification-channels/{name}/probe",
		"/api/v2/media-managers", "/api/v2/media-managers/{name}",
		"/api/v2/media-managers/{name}/probe", "/api/v2/media-managers/{name}/lookup",
		"/api/v2/metadata-providers", "/api/v2/metadata-providers/{name}",
		"/api/v2/metadata-providers/{name}/probe", "/api/v2/metadata-providers/{name}/resolve",
		"/api/v2/image-hosts/{name}/probe",
		"/api/v2/downloader-adapters",
		"/api/v2/migrations/legacy/preview", "/api/v2/migrations/legacy",
		"/api/v2/migrations/legacy/{import_id}",
		"/api/v2/operational-logs", "/api/v2/operational-logs/{log_id}/context", "/api/v2/operational-logs/stream", "/api/v2/operational-logs/export",
		"/api/v2/incidents", "/api/v2/incidents/{incident_id}", "/api/v2/incidents/{incident_id}/acknowledge", "/api/v2/incidents/{incident_id}/resolve",
		"/api/v2/llm-providers", "/api/v2/llm-providers/{provider_id}", "/api/v2/llm-providers/{provider_id}/probe",
		"/api/v2/site-rules/analyze", "/api/v2/site-rules/analyze/stream", "/api/v2/site-rules/analyze/result",
		"/api/v2/diagnostics", "/api/v2/diagnostics/{diagnostic_id}", "/api/v2/diagnostics/{diagnostic_id}/messages",
		"/api/v2/operations/overview", "/api/v2/operations/settings", "/api/v2/api-tokens", "/api/v2/api-tokens/{token_id}",
		"/api/v2/backups/policy", "/api/v2/backups/runs", "/api/v2/backups", "/api/v2/backups/{backup_id}/verify",
	}
	for _, path := range requiredPaths {
		if _, exists := document.Paths[path]; !exists {
			t.Errorf("OpenAPI path %s is missing", path)
		}
	}
	for _, schema := range []string{"RetorrentSummaryArtifact", "RetorrentSummary", "JobSummaryValue", "ReplayJobRequest", "StepAttempt", "StepAttemptListEnvelope", "LiveReadinessCheck", "LiveRuleConfirmation", "LiveReadinessReport", "JobAttentionEnvelope", "UploadPreviewEnvelope", "SiteAccessPolicyEnvelope", "LLMProviderInput", "SiteRuleAnalysisInput", "RuleSourceSetInput", "RuleSourceSet", "RuleCollectionRun", "RuleCollectionEnvelope", "OperationsSettings", "BackupPolicyInput"} {
		if len(document.Components.Schemas[schema]) == 0 {
			t.Errorf("OpenAPI schema %s is missing", schema)
		}
	}
	for _, schema := range []string{"RetorrentDownloader", "AddTorrentRequest"} {
		var decoded struct {
			Properties map[string]json.RawMessage `json:"properties"`
		}
		if err := json.Unmarshal(document.Components.Schemas[schema], &decoded); err != nil || len(decoded.Properties["apply_labels"]) == 0 {
			t.Errorf("OpenAPI schema %s is missing apply_labels: %v", schema, err)
		}
	}
	var summarySchema struct {
		Required []string `json:"required"`
	}
	if err := json.Unmarshal(document.Components.Schemas["RetorrentSummary"], &summarySchema); err != nil {
		t.Fatalf("decode RetorrentSummary schema: %v", err)
	}
	for _, field := range []string{"source", "target", "seeding", "audit", "summary_file"} {
		if !containsString(summarySchema.Required, field) {
			t.Errorf("RetorrentSummary required field %s is missing", field)
		}
	}

	tools := toolDefinitions()
	if len(tools) != 68 {
		t.Fatalf("tool count = %d, want 68", len(tools))
	}
	seen := make(map[string]struct{}, len(tools))
	for _, tool := range tools {
		if tool.Name == "" || tool.Method == "" || tool.Path == "" || tool.InputSchema == nil || len(tool.RequiredScopes) == 0 {
			t.Errorf("incomplete tool definition: %#v", tool)
		}
		if _, exists := seen[tool.Name]; exists {
			t.Errorf("duplicate tool name %s", tool.Name)
		}
		seen[tool.Name] = struct{}{}
		if _, exists := document.Paths[tool.Path]; !exists {
			t.Errorf("tool %s references undocumented path %s", tool.Name, tool.Path)
		}
		if strings.HasPrefix(tool.Path, "/api/v2/api-tokens") || strings.HasPrefix(tool.Path, "/api/v2/llm-providers") ||
			strings.HasPrefix(tool.Path, "/api/v2/backups") || strings.HasPrefix(tool.Path, "/api/v2/incidents/") &&
			(strings.HasSuffix(tool.Path, "/acknowledge") || strings.HasSuffix(tool.Path, "/resolve")) {
			t.Errorf("privileged operations tool must not be agent-exposed: %s", tool.Name)
		}
	}

	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(),
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
	})
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if response.Code != http.StatusOK || response.Header().Get("Content-Type") != "application/vnd.oai.openapi+json;version=3.1" {
		t.Fatalf("OpenAPI HTTP response = %d/%s", response.Code, response.Header().Get("Content-Type"))
	}
}

type capturedLogWriter struct{ entries chan operations.LogEntry }

func (writer capturedLogWriter) InsertLog(_ context.Context, entry operations.LogEntry) (int64, error) {
	writer.entries <- entry
	return 1, nil
}

func TestRequestCorrelationAndHTTPLogFields(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	writer := capturedLogWriter{entries: make(chan operations.LogEntry, 4)}
	sink := operations.NewAsyncLogSink(writer, slog.New(slog.NewTextHandler(io.Discard, nil)), 4)
	go sink.Run(ctx)
	t.Cleanup(func() { cancel(); sink.Wait() })
	handler := New(Dependencies{Database: fakeDatabase{}, DataDir: t.TempDir(), LogSink: sink,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"}})
	request := httptest.NewRequest(http.MethodGet, "/api/v2/version", nil)
	request.Header.Set("X-Request-ID", "fixture.request-42")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Header().Get("X-Request-ID") != "fixture.request-42" || response.Header().Get("X-Trace-ID") == "" {
		t.Fatalf("correlation headers = %#v", response.Header())
	}
	select {
	case entry := <-writer.entries:
		if entry.RequestID != "fixture.request-42" || entry.TraceID == "" || entry.Route != "GET /api/v2/version" || entry.StatusCode != 200 || entry.ResponseBytes == 0 {
			t.Fatalf("operational HTTP log = %#v", entry)
		}
	case <-time.After(time.Second):
		t.Fatal("operational HTTP log was not persisted")
	}
	bad := httptest.NewRequest(http.MethodGet, "/health/live", nil)
	bad.Header.Set("X-Request-ID", "contains whitespace")
	health := httptest.NewRecorder()
	handler.ServeHTTP(health, bad)
	if health.Header().Get("X-Request-ID") == "contains whitespace" || health.Header().Get("X-Request-ID") == "" {
		t.Fatalf("invalid request ID was not replaced: %q", health.Header().Get("X-Request-ID"))
	}
	select {
	case entry := <-writer.entries:
		t.Fatalf("successful health check was persisted: %#v", entry)
	case <-time.After(50 * time.Millisecond):
	}
}

func TestHTTPErrorLogKeepsRedactedFailureReason(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	writer := capturedLogWriter{entries: make(chan operations.LogEntry, 2)}
	sink := operations.NewAsyncLogSink(writer, slog.New(slog.NewTextHandler(io.Discard, nil)), 2)
	go sink.Run(ctx)
	t.Cleanup(func() { cancel(); sink.Wait() })
	handler := requestCorrelation(requestLogger(slog.New(slog.NewTextHandler(io.Discard, nil)), sink, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeProblem(w, http.StatusGatewayTimeout, "provider_timeout", "provider request timed out after 60 seconds; api_key=fixture-secret")
	})))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/api/v2/site-rules/analyze", nil))
	select {
	case entry := <-writer.entries:
		if entry.ErrorCode != "provider_timeout" || !strings.Contains(string(entry.Attributes), "provider request timed out after 60 seconds") || strings.Contains(string(entry.Attributes), "fixture-secret") {
			t.Fatalf("error log = %#v", entry)
		}
	case <-time.After(time.Second):
		t.Fatal("error log was not persisted")
	}
}

func TestAgentDiscoveryAndSkillArePublic(t *testing.T) {
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(),
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"*"}}},
	})

	discoveryResponse := httptest.NewRecorder()
	handler.ServeHTTP(discoveryResponse, httptest.NewRequest(http.MethodGet, agentDiscoveryPath, nil))
	if discoveryResponse.Code != http.StatusOK || discoveryResponse.Header().Get("Content-Type") != "application/json; charset=utf-8" {
		t.Fatalf("discovery response = %d/%s", discoveryResponse.Code, discoveryResponse.Header().Get("Content-Type"))
	}
	var discovery struct {
		Kind       string `json:"kind"`
		SkillURL   string `json:"skill_url"`
		OpenAPIURL string `json:"openapi_url"`
		ToolsURL   string `json:"tools_url"`
	}
	if err := json.Unmarshal(discoveryResponse.Body.Bytes(), &discovery); err != nil {
		t.Fatal(err)
	}
	if discovery.Kind != "upload-assistant.agent-discovery.v1" || discovery.SkillURL != agentSkillPath || discovery.OpenAPIURL != "/openapi.json" || discovery.ToolsURL != "/api/v2/tools" {
		t.Fatalf("unexpected discovery: %#v", discovery)
	}

	skillResponse := httptest.NewRecorder()
	handler.ServeHTTP(skillResponse, httptest.NewRequest(http.MethodGet, agentSkillPath, nil))
	if skillResponse.Code != http.StatusOK || skillResponse.Header().Get("Content-Type") != "text/markdown; charset=utf-8" {
		t.Fatalf("skill response = %d/%s", skillResponse.Code, skillResponse.Header().Get("Content-Type"))
	}
	if body := skillResponse.Body.String(); !strings.HasPrefix(body, "---\nname: upload-assistant\n") || !strings.Contains(body, "Never bypass rule acceptance") {
		t.Fatalf("unexpected skill body: %.120q", body)
	}

	toolsResponse := httptest.NewRecorder()
	handler.ServeHTTP(toolsResponse, httptest.NewRequest(http.MethodGet, "/api/v2/tools", nil))
	if toolsResponse.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated tools status = %d, want %d", toolsResponse.Code, http.StatusUnauthorized)
	}
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}

func TestProtectedPathsRequireBearerToken(t *testing.T) {
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(),
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"*"}}},
	})
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/v2/protected", nil))
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("missing token status = %d, want %d", response.Code, http.StatusUnauthorized)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/protected", nil)
	request.Header.Set("Authorization", "Bearer ua_test-token-value-that-is-long-enough")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("authenticated status = %d, want %d", response.Code, http.StatusNotFound)
	}
}

func TestReadinessRequiresDatabaseAndDataDirectory(t *testing.T) {
	tests := []struct {
		name     string
		database fakeDatabase
		dataDir  string
		wantCode int
	}{
		{name: "ready", database: fakeDatabase{}, dataDir: t.TempDir(), wantCode: http.StatusOK},
		{name: "database down", database: fakeDatabase{err: errors.New("down")}, dataDir: t.TempDir(), wantCode: http.StatusServiceUnavailable},
		{name: "data directory missing", database: fakeDatabase{}, dataDir: "/path/that/does/not/exist", wantCode: http.StatusServiceUnavailable},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			handler := New(Dependencies{
				Database: test.database,
				DataDir:  test.dataDir,
				Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
				Build:    buildinfo.Info{Version: "test"},
			})
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health/ready", nil))
			if response.Code != test.wantCode {
				t.Fatalf("status = %d, want %d", response.Code, test.wantCode)
			}
		})
	}
}
