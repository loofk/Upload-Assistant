package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type IntegrationService interface {
	PutSiteCredential(context.Context, string, string, string, workflow.Actor) (integrations.SiteCredential, error)
	ListSiteCredentials(context.Context, string) ([]integrations.SiteCredential, error)
	DisableSiteCredential(context.Context, string, string, workflow.Actor) (integrations.SiteCredential, error)
	UpsertDownloader(context.Context, string, integrations.DownloaderInput, workflow.Actor) (integrations.Downloader, error)
	ListDownloaders(context.Context) ([]integrations.Downloader, error)
	UpsertImageHost(context.Context, string, integrations.ImageHostInput, workflow.Actor) (integrations.ImageHost, error)
	ListImageHosts(context.Context) ([]integrations.ImageHost, error)
	UpsertNotificationChannel(context.Context, string, integrations.NotificationChannelInput, workflow.Actor) (integrations.NotificationChannel, error)
	ListNotificationChannels(context.Context) ([]integrations.NotificationChannel, error)
	UpsertMediaManager(context.Context, string, integrations.MediaManagerInput, workflow.Actor) (integrations.MediaManager, error)
	ListMediaManagers(context.Context) ([]integrations.MediaManager, error)
	CreateScreenshotProfile(context.Context, integrations.ScreenshotProfileInput, workflow.Actor) (integrations.ScreenshotProfile, error)
	ListScreenshotProfiles(context.Context) ([]integrations.ScreenshotProfile, error)
}

type integrationsAPI struct {
	service IntegrationService
}

type siteCredentialRequest struct {
	Value string `json:"value"`
}

func registerIntegrationRoutes(mux *http.ServeMux, service IntegrationService) {
	api := integrationsAPI{service: service}
	mux.HandleFunc("GET /api/v2/sites/{site_code}/credentials", api.listSiteCredentials)
	mux.HandleFunc("PUT /api/v2/sites/{site_code}/credentials/{credential_name}", api.putSiteCredential)
	mux.HandleFunc("POST /api/v2/sites/{site_code}/credentials/{credential_name}/disable", api.disableSiteCredential)
	mux.HandleFunc("GET /api/v2/downloaders", api.listDownloaders)
	mux.HandleFunc("GET /api/v2/downloader-adapters", api.listDownloaderAdapters)
	mux.HandleFunc("PUT /api/v2/downloaders/{name}", api.putDownloader)
	mux.HandleFunc("GET /api/v2/image-hosts", api.listImageHosts)
	mux.HandleFunc("PUT /api/v2/image-hosts/{name}", api.putImageHost)
	mux.HandleFunc("GET /api/v2/screenshot-profiles", api.listScreenshotProfiles)
	mux.HandleFunc("POST /api/v2/screenshot-profiles", api.createScreenshotProfile)
	mux.HandleFunc("GET /api/v2/notification-channels", api.listNotificationChannels)
	mux.HandleFunc("PUT /api/v2/notification-channels/{name}", api.putNotificationChannel)
	mux.HandleFunc("GET /api/v2/media-managers", api.listMediaManagers)
	mux.HandleFunc("PUT /api/v2/media-managers/{name}", api.putMediaManager)
}

func (api integrationsAPI) listNotificationChannels(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	items, err := api.service.ListNotificationChannels(r.Context())
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "ready", "notification_channels": items, "blockers": []any{}, "next_actions": []any{}})
}

func (api integrationsAPI) putNotificationChannel(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request integrations.NotificationChannelInput
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	item, err := api.service.UpsertNotificationChannel(r.Context(), strings.TrimSpace(r.PathValue("name")), request, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, integrationEnvelope("configured", "notification_channel", item))
}

func (api integrationsAPI) listMediaManagers(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	items, err := api.service.ListMediaManagers(r.Context())
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "ready", "media_managers": items, "blockers": []any{}, "next_actions": []any{}})
}

func (api integrationsAPI) putMediaManager(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request integrations.MediaManagerInput
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	item, err := api.service.UpsertMediaManager(r.Context(), strings.TrimSpace(r.PathValue("name")), request, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, integrationEnvelope("configured", "media_manager", item))
}

func (api integrationsAPI) listDownloaderAdapters(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	items := integrations.DownloaderAdapterCapabilities()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "count": len(items), "adapters": items,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api integrationsAPI) listSiteCredentials(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	siteCode := strings.ToUpper(strings.TrimSpace(r.PathValue("site_code")))
	credentials, err := api.service.ListSiteCredentials(r.Context(), siteCode)
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "site_code": siteCode,
		"credentials": credentials, "blockers": []any{}, "next_actions": []any{},
	})
}

func (api integrationsAPI) putSiteCredential(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request siteCredentialRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	credential, err := api.service.PutSiteCredential(
		r.Context(), strings.ToUpper(strings.TrimSpace(r.PathValue("site_code"))),
		strings.ToLower(strings.TrimSpace(r.PathValue("credential_name"))), request.Value,
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, integrationEnvelope("configured", "credential", credential))
}

func (api integrationsAPI) disableSiteCredential(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	credential, err := api.service.DisableSiteCredential(
		r.Context(), strings.ToUpper(strings.TrimSpace(r.PathValue("site_code"))),
		strings.ToLower(strings.TrimSpace(r.PathValue("credential_name"))),
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, integrationEnvelope("disabled", "credential", credential))
}

func (api integrationsAPI) listDownloaders(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	items, err := api.service.ListDownloaders(r.Context())
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "downloaders": items,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api integrationsAPI) putDownloader(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "downloader:manage")
	if !ok {
		return
	}
	var request integrations.DownloaderInput
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	item, err := api.service.UpsertDownloader(
		r.Context(), strings.TrimSpace(r.PathValue("name")), request,
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, integrationEnvelope("configured", "downloader", item))
}

func (api integrationsAPI) listImageHosts(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	items, err := api.service.ListImageHosts(r.Context())
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "image_hosts": items,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api integrationsAPI) putImageHost(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request integrations.ImageHostInput
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	item, err := api.service.UpsertImageHost(
		r.Context(), strings.TrimSpace(r.PathValue("name")), request,
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, integrationEnvelope("configured", "image_host", item))
}

func (api integrationsAPI) listScreenshotProfiles(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	items, err := api.service.ListScreenshotProfiles(r.Context())
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "screenshot_profiles": items,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api integrationsAPI) createScreenshotProfile(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request integrations.ScreenshotProfileInput
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	item, err := api.service.CreateScreenshotProfile(
		r.Context(), request, workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeIntegrationError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, integrationEnvelope("created", "screenshot_profile", item))
}

func integrationEnvelope(status, field string, value any) map[string]any {
	return map[string]any{
		"ok": true, "status": status, field: value,
		"blockers": []any{}, "next_actions": []any{},
	}
}

func writeIntegrationError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "integration_not_found", "the requested integration resource was not found")
	case errors.Is(err, integrations.ErrValidation):
		writeProblem(w, http.StatusBadRequest, "invalid_integration_config", err.Error())
	default:
		writeProblem(w, http.StatusInternalServerError, "internal_error", "integration request could not be completed")
	}
}
