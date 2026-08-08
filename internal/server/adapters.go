package server

import (
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

func registerAdapterCatalogRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v2/adapters", func(w http.ResponseWriter, r *http.Request) {
		if _, ok := requireScope(w, r, "config:read"); !ok {
			return
		}
		kind := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("kind")))
		catalog := integrations.AdapterCapabilities()
		adapters := catalog.Adapters
		if kind != "" {
			filtered := make([]integrations.AdapterCapability, 0, len(adapters))
			for _, adapter := range adapters {
				if adapter.Kind == kind {
					filtered = append(filtered, adapter)
				}
			}
			if len(filtered) == 0 {
				writeProblem(w, http.StatusBadRequest, "invalid_adapter_kind", "kind does not match a published adapter capability")
				return
			}
			adapters = filtered
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok": true, "status": "ready", "catalog_version": catalog.Version,
			"catalog_sha256": catalog.SHA256, "count": len(adapters), "adapters": adapters,
			"blockers": []any{}, "next_actions": []any{},
		})
	})
}
