package server

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/metadataproviders"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeMetadataProviderService struct {
	request metadataproviders.ResolveRequest
	err     error
}

func (service *fakeMetadataProviderService) Probe(_ context.Context, name string, _ workflow.Actor) (metadataproviders.ProbeResult, error) {
	if service.err != nil {
		return metadataproviders.ProbeResult{}, service.err
	}
	return metadataproviders.ProbeResult{Name: name, Adapter: "tmdb", Status: "ready", Matched: true}, nil
}

func TestMetadataProviderProbeReturnsActionableWorkersDevFailure(t *testing.T) {
	service := &fakeMetadataProviderService{err: &metadataproviders.ProviderError{
		Code:    "provider_workers_dev_unreachable",
		Message: "the configured workers.dev endpoint is unreachable from this runtime; bind the Worker to a reachable custom domain and save its /api URL",
	}}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Metadata: service,
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"config:manage"}}},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/v2/metadata-providers/ptgen-main/probe", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadGateway || !bytes.Contains(response.Body.Bytes(), []byte(`"code":"metadata_provider_workers_dev_unreachable"`)) || !bytes.Contains(response.Body.Bytes(), []byte("custom domain")) {
		t.Fatalf("probe = %d %s", response.Code, response.Body.String())
	}
}

func (service *fakeMetadataProviderService) Resolve(_ context.Context, name string, request metadataproviders.ResolveRequest, _ workflow.Actor) (metadataproviders.ResolveResult, error) {
	service.request = request
	return metadataproviders.ResolveResult{Name: name, Adapter: "tmdb", Matched: true, Identity: metadataproviders.Identity{IMDbID: request.IMDbID, TMDbID: "42", TMDbType: "movie"}}, nil
}

func TestMetadataProviderProbeRoute(t *testing.T) {
	service := &fakeMetadataProviderService{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Metadata: service,
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"config:manage"}}},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/v2/metadata-providers/tmdb-main/probe", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !bytes.Contains(response.Body.Bytes(), []byte(`"matched":true`)) {
		t.Fatalf("probe = %d %s", response.Code, response.Body.String())
	}
}

func TestMetadataProviderResolveRoute(t *testing.T) {
	service := &fakeMetadataProviderService{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Metadata: service,
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "operator", TokenScopes: []string{"jobs:write"}}},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/v2/metadata-providers/tmdb-main/resolve", bytes.NewBufferString(`{"imdb_id":"tt1234567"}`))
	request.Header.Set("Authorization", "Bearer fixture")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || service.request.IMDbID != "tt1234567" || !bytes.Contains(response.Body.Bytes(), []byte(`"status":"ready"`)) {
		t.Fatalf("response/request = %d %s / %#v", response.Code, response.Body.String(), service.request)
	}
}
