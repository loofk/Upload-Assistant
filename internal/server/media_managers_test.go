package server

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/mediamanagers"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeMediaManagerService struct{ request mediamanagers.LookupRequest }

func (service *fakeMediaManagerService) Probe(context.Context, string, workflow.Actor) (mediamanagers.ProbeResult, error) {
	return mediamanagers.ProbeResult{Name: "sonarr-main", Adapter: "sonarr", Status: "ready", Version: "4.0.15", ResponseSHA256: "fixture"}, nil
}

func (service *fakeMediaManagerService) Lookup(_ context.Context, _ string, request mediamanagers.LookupRequest, _ workflow.Actor) (mediamanagers.LookupResult, error) {
	service.request = request
	return mediamanagers.LookupResult{Name: "sonarr-main", Adapter: "sonarr", Matched: true, Metadata: mediamanagers.Metadata{TVDBID: 123, IMDbID: "tt0011223"}, QuerySHA256: "query", ResponseSHA256: "response"}, nil
}

func TestMediaManagerProbeAndLookupRoutes(t *testing.T) {
	service := &fakeMediaManagerService{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), MediaManagers: service,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "2cbfe1ba-d85c-4ab8-b529-50cdacb87a03", Role: "admin", TokenScopes: []string{"*"}}},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/v2/media-managers/sonarr-main/probe", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !bytes.Contains(response.Body.Bytes(), []byte(`"version":"4.0.15"`)) {
		t.Fatalf("probe = %d %s", response.Code, response.Body.String())
	}
	request = httptest.NewRequest(http.MethodPost, "/api/v2/media-managers/sonarr-main/lookup", bytes.NewBufferString(`{"path":"/downloads/show.mkv","title":"show"}`))
	request.Header.Set("Authorization", "Bearer fixture")
	request.Header.Set("Content-Type", "application/json")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || service.request.Path != "/downloads/show.mkv" || !bytes.Contains(response.Body.Bytes(), []byte(`"matched":true`)) {
		t.Fatalf("lookup = %d %s, request=%#v", response.Code, response.Body.String(), service.request)
	}
}
