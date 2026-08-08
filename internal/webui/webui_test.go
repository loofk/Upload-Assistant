package webui

import (
	"net/http"
	"net/http/httptest"
	"regexp"
	"testing"
)

func TestEmbeddedUIIncludesIndexAndHashedAssets(t *testing.T) {
	handler := Handler()
	index := httptest.NewRecorder()
	handler.ServeHTTP(index, httptest.NewRequest(http.MethodGet, "/", nil))
	if index.Code != http.StatusOK || index.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("index status/cache = %d/%s", index.Code, index.Header().Get("Cache-Control"))
	}
	match := regexp.MustCompile(`src="(/assets/[^"]+\.js)"`).FindStringSubmatch(index.Body.String())
	if len(match) != 2 {
		t.Fatalf("compiled JavaScript asset is missing from index: %s", index.Body.String())
	}
	asset := httptest.NewRecorder()
	handler.ServeHTTP(asset, httptest.NewRequest(http.MethodGet, match[1], nil))
	if asset.Code != http.StatusOK || asset.Header().Get("Cache-Control") != "public, max-age=31536000, immutable" || asset.Body.Len() == 0 {
		t.Fatalf("asset status/cache/size = %d/%s/%d", asset.Code, asset.Header().Get("Cache-Control"), asset.Body.Len())
	}
}

func TestEmbeddedUIDoesNotListAssetDirectory(t *testing.T) {
	response := httptest.NewRecorder()
	Handler().ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/assets/", nil))
	if response.Code != http.StatusNotFound {
		t.Fatalf("asset directory status = %d, want 404", response.Code)
	}
}
