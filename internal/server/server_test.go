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

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
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
		"/api/v2/candidates/{candidate_id}/retorrent-job",
		"/api/v2/jobs/{job_id}", "/api/v2/jobs/{job_id}/summary",
		"/api/v2/jobs/{job_id}/artifacts/{artifact_id}/content",
		"/api/v2/jobs/{job_id}/resume", "/api/v2/sites/{site_code}/rules/active",
		"/api/v2/site-rules/{revision_id}/approve",
		"/api/v2/schedules/daily-candidates", "/api/v2/schedules/daily-candidates/{schedule_id}",
		"/api/v2/schedules/daily-candidates/{schedule_id}/runs",
		"/api/v2/notifications",
		"/api/v2/downloader-adapters",
		"/api/v2/migrations/legacy/preview", "/api/v2/migrations/legacy",
		"/api/v2/migrations/legacy/{import_id}",
	}
	for _, path := range requiredPaths {
		if _, exists := document.Paths[path]; !exists {
			t.Errorf("OpenAPI path %s is missing", path)
		}
	}
	for _, schema := range []string{"RetorrentSummaryArtifact", "RetorrentSummary", "JobSummaryValue"} {
		if len(document.Components.Schemas[schema]) == 0 {
			t.Errorf("OpenAPI schema %s is missing", schema)
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
	if len(tools) != 30 {
		t.Fatalf("tool count = %d, want 30", len(tools))
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
