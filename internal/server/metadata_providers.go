package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/metadataproviders"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type MetadataProviderService interface {
	Resolve(context.Context, string, metadataproviders.ResolveRequest, workflow.Actor) (metadataproviders.ResolveResult, error)
}

type metadataProviderAPI struct{ service MetadataProviderService }

func registerMetadataProviderRoutes(mux *http.ServeMux, service MetadataProviderService) {
	api := metadataProviderAPI{service: service}
	mux.HandleFunc("POST /api/v2/metadata-providers/{name}/resolve", api.resolve)
}

func (api metadataProviderAPI) resolve(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	var request metadataproviders.ResolveRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	result, err := api.service.Resolve(r.Context(), strings.TrimSpace(r.PathValue("name")), request, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeMetadataProviderError(w, err)
		return
	}
	status := "ready"
	nextActions := []any{}
	if !result.Matched {
		status = "not_found"
		nextActions = []any{map[string]any{"action": "provide_metadata_identity_or_try_another_provider"}}
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": status, "resolution": result, "blockers": []any{}, "next_actions": nextActions})
}

func writeMetadataProviderError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "metadata_provider_not_found", "the requested metadata provider was not found")
	case errors.Is(err, integrations.ErrValidation), errors.Is(err, metadataproviders.ErrValidation):
		writeProblem(w, http.StatusBadRequest, "invalid_metadata_provider_request", err.Error())
	default:
		writeProblem(w, http.StatusBadGateway, "metadata_provider_request_failed", "the metadata provider request failed; inspect its health and audit events")
	}
}
