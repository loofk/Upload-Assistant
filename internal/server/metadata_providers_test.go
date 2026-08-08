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
}

func (service *fakeMetadataProviderService) Resolve(_ context.Context, name string, request metadataproviders.ResolveRequest, _ workflow.Actor) (metadataproviders.ResolveResult, error) {
	service.request = request
	return metadataproviders.ResolveResult{Name: name, Adapter: "tmdb", Matched: true, Identity: metadataproviders.Identity{IMDbID: request.IMDbID, TMDbID: "42", TMDbType: "movie"}}, nil
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
