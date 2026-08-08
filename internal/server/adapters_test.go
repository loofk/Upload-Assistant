package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

func TestAdapterCatalogEndpointIsStableAndFilterable(t *testing.T) {
	mux := http.NewServeMux()
	registerAdapterCatalogRoutes(mux)
	request := httptest.NewRequest(http.MethodGet, "/api/v2/adapters?kind=site", nil)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "auditor", Role: "auditor", TokenScopes: []string{"config:read"},
	}))
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status/body = %d/%s", response.Code, response.Body.String())
	}
	var body struct {
		OK             bool                             `json:"ok"`
		Status         string                           `json:"status"`
		CatalogVersion string                           `json:"catalog_version"`
		CatalogSHA256  string                           `json:"catalog_sha256"`
		Count          int                              `json:"count"`
		Adapters       []integrations.AdapterCapability `json:"adapters"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if !body.OK || body.Status != "ready" || body.CatalogVersion != integrations.AdapterCatalogVersion || body.CatalogSHA256 != integrations.AdapterCapabilities().SHA256 || body.Count != 11 {
		t.Fatalf("catalog response = %#v", body)
	}
	for _, adapter := range body.Adapters {
		if adapter.Kind != "site" || adapter.CredentialFields == nil || adapter.Operations == nil || adapter.SafetyGates == nil || adapter.Constraints == nil {
			t.Fatalf("site capability contract = %#v", adapter)
		}
	}
}

func TestAdapterCatalogRejectsUnknownKind(t *testing.T) {
	mux := http.NewServeMux()
	registerAdapterCatalogRoutes(mux)
	request := httptest.NewRequest(http.MethodGet, "/api/v2/adapters?kind=imaginary", nil)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "auditor", Role: "auditor", TokenScopes: []string{"config:read"},
	}))
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || response.Body.String() == "" {
		t.Fatalf("status/body = %d/%s", response.Code, response.Body.String())
	}
}
