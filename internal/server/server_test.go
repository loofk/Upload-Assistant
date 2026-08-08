package server

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
)

type fakeDatabase struct{ err error }

func (f fakeDatabase) Ping(context.Context) error { return f.err }

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
