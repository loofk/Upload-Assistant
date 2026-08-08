package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/mediamanagers"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type MediaManagerService interface {
	Probe(context.Context, string, workflow.Actor) (mediamanagers.ProbeResult, error)
	Lookup(context.Context, string, mediamanagers.LookupRequest, workflow.Actor) (mediamanagers.LookupResult, error)
}

type mediaManagerAPI struct{ service MediaManagerService }

func registerMediaManagerRoutes(mux *http.ServeMux, service MediaManagerService) {
	api := mediaManagerAPI{service: service}
	mux.HandleFunc("POST /api/v2/media-managers/{name}/probe", api.probe)
	mux.HandleFunc("POST /api/v2/media-managers/{name}/lookup", api.lookup)
}

func (api mediaManagerAPI) probe(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	result, err := api.service.Probe(r.Context(), strings.TrimSpace(r.PathValue("name")), workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeMediaManagerError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "ready", "probe": result, "blockers": []any{}, "next_actions": []any{}})
}

func (api mediaManagerAPI) lookup(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	var request mediamanagers.LookupRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	result, err := api.service.Lookup(r.Context(), strings.TrimSpace(r.PathValue("name")), request, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeMediaManagerError(w, err)
		return
	}
	status := "ready"
	nextActions := []any{}
	if !result.Matched {
		status = "not_found"
		nextActions = []any{map[string]any{"action": "continue_without_media_manager_metadata"}}
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": status, "lookup": result, "blockers": []any{}, "next_actions": nextActions})
}

func writeMediaManagerError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "media_manager_not_found", "the requested media manager was not found")
	case errors.Is(err, integrations.ErrValidation), errors.Is(err, mediamanagers.ErrValidation):
		writeProblem(w, http.StatusBadRequest, "invalid_media_manager_request", err.Error())
	default:
		writeProblem(w, http.StatusBadGateway, "media_manager_request_failed", "the media manager request failed; inspect its health and audit event")
	}
}
