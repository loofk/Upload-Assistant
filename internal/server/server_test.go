package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
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

func TestOpenAPIAndToolContracts(t *testing.T) {
	var document struct {
		OpenAPI string                    `json:"openapi"`
		Paths   map[string]map[string]any `json:"paths"`
	}
	if err := json.Unmarshal(openAPIDocument, &document); err != nil {
		t.Fatalf("embedded OpenAPI is invalid JSON: %v", err)
	}
	if document.OpenAPI != "3.1.0" {
		t.Fatalf("openapi = %s", document.OpenAPI)
	}
	requiredPaths := []string{
		"/openapi.json", "/api/v2/tools", "/api/v2/jobs",
		"/api/v2/jobs/{job_id}", "/api/v2/jobs/{job_id}/summary",
		"/api/v2/jobs/{job_id}/resume", "/api/v2/sites/{site_code}/rules/active",
		"/api/v2/site-rules/{revision_id}/approve",
	}
	for _, path := range requiredPaths {
		if _, exists := document.Paths[path]; !exists {
			t.Errorf("OpenAPI path %s is missing", path)
		}
	}

	tools := toolDefinitions()
	if len(tools) != 15 {
		t.Fatalf("tool count = %d, want 15", len(tools))
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
