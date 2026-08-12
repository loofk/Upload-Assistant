package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/notifications"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type NotificationProbeService interface {
	Probe(context.Context, string, workflow.Actor) (notifications.ProbeResult, error)
}

type notificationProbeAPI struct{ service NotificationProbeService }

type notificationProbeRequest struct {
	ConfirmDelivery bool `json:"confirm_delivery"`
}

func registerNotificationProbeRoutes(mux *http.ServeMux, service NotificationProbeService) {
	api := notificationProbeAPI{service: service}
	mux.HandleFunc("POST /api/v2/notification-channels/{name}/probe", api.probe)
}

func (api notificationProbeAPI) probe(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request notificationProbeRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if !request.ConfirmDelivery {
		writeProblem(w, http.StatusBadRequest, "notification_probe_confirmation_required", "confirm_delivery=true is required because this test sends a remote message")
		return
	}
	result, err := api.service.Probe(r.Context(), strings.TrimSpace(r.PathValue("name")), workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeNotificationProbeError(w, result, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": result.Status, "probe": result, "blockers": []any{}, "next_actions": []any{}})
}

func writeNotificationProbeError(w http.ResponseWriter, result notifications.ProbeResult, err error) {
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "notification_channel_not_found", "the requested notification channel was not found")
	case errors.Is(err, integrations.ErrValidation):
		writeProblem(w, http.StatusBadRequest, "invalid_notification_probe", err.Error())
	case errors.Is(err, notifications.ErrDeliveryOutcomeUnknown):
		writeJSON(w, http.StatusConflict, map[string]any{
			"ok": false, "status": "blocked", "code": "notification_probe_outcome_unknown",
			"message": "the test message may have been delivered; inspect the notification record before reconciling or testing again",
			"probe":   result, "blockers": []map[string]any{{"code": "notification_probe_outcome_unknown"}},
			"next_actions": []map[string]any{{"action": "inspect_notification_delivery", "notification_id": result.NotificationID}},
		})
	default:
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"ok": false, "status": "failed", "code": "notification_probe_failed",
			"message": "the notification channel rejected the test message or returned an invalid receipt",
			"probe":   result, "blockers": []any{}, "next_actions": []map[string]any{{"action": "review_notification_configuration"}},
		})
	}
}
