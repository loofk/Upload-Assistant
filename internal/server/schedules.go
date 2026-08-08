package server

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/schedules"
)

type ScheduleService interface {
	Create(context.Context, schedules.CreateInput, time.Time) (schedules.Schedule, error)
	List(context.Context, int) ([]schedules.Schedule, error)
	Update(context.Context, string, schedules.UpdateInput, time.Time) (schedules.Schedule, error)
	ListRuns(context.Context, string, int) ([]schedules.Run, error)
	ListNotifications(context.Context, int) ([]schedules.Notification, error)
	ReconcileNotification(context.Context, string, schedules.NotificationReconciliationInput, time.Time) (schedules.Notification, error)
}

type scheduleAPI struct{ service ScheduleService }

type createScheduleRequest struct {
	Name           string                         `json:"name"`
	CronExpression string                         `json:"cron_expression"`
	Timezone       string                         `json:"timezone"`
	Enabled        *bool                          `json:"enabled,omitempty"`
	Config         schedules.DailyCandidateConfig `json:"config"`
}

type updateScheduleRequest struct {
	CronExpression *string                         `json:"cron_expression,omitempty"`
	Timezone       *string                         `json:"timezone,omitempty"`
	Enabled        *bool                           `json:"enabled,omitempty"`
	Config         *schedules.DailyCandidateConfig `json:"config,omitempty"`
}

func registerScheduleRoutes(mux *http.ServeMux, service ScheduleService) {
	api := scheduleAPI{service: service}
	mux.HandleFunc("GET /api/v2/schedules/daily-candidates", api.list)
	mux.HandleFunc("POST /api/v2/schedules/daily-candidates", api.create)
	mux.HandleFunc("PATCH /api/v2/schedules/daily-candidates/{schedule_id}", api.update)
	mux.HandleFunc("GET /api/v2/schedules/daily-candidates/{schedule_id}/runs", api.runs)
	mux.HandleFunc("GET /api/v2/notifications", api.notifications)
	mux.HandleFunc("POST /api/v2/notifications/{notification_id}/reconcile", api.reconcileNotification)
}

func (api scheduleAPI) runs(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id := r.PathValue("schedule_id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_schedule_id", "schedule_id must be a UUID")
		return
	}
	limit, err := parseIntQuery(r, "limit", 25, 1, 100)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	runs, err := api.service.ListRuns(r.Context(), id, limit)
	if err != nil {
		writeScheduleError(w, err)
		return
	}
	for index := range runs {
		if runs[index].LastError != "" {
			runs[index].LastError = "scheduled daily candidate job creation failed"
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "schedule_id": id, "count": len(runs), "runs": runs,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api scheduleAPI) list(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 50, 1, 100)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	items, err := api.service.List(r.Context(), limit)
	if err != nil {
		writeScheduleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "count": len(items), "schedules": items,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api scheduleAPI) create(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	var request createScheduleRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	enabled := true
	if request.Enabled != nil {
		enabled = *request.Enabled
	}
	item, err := api.service.Create(r.Context(), schedules.CreateInput{
		Name: request.Name, CronExpression: request.CronExpression, Timezone: request.Timezone,
		Enabled: enabled, Config: request.Config, CreatedBy: principal.UserID,
	}, time.Now().UTC())
	if err != nil {
		writeScheduleError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"ok": true, "status": "ready", "schedule_id": item.ID, "schedule": item,
		"blockers": []any{}, "next_actions": []map[string]any{{"action": "wait_for_next_run", "parameters": map[string]any{"next_run_at": item.NextRunAt}}},
		"safety": map[string]any{"submits_candidates": false, "uploads_torrents": false},
	})
}

func (api scheduleAPI) update(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:write"); !ok {
		return
	}
	id := r.PathValue("schedule_id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_schedule_id", "schedule_id must be a UUID")
		return
	}
	var request updateScheduleRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if request.CronExpression == nil && request.Timezone == nil && request.Enabled == nil && request.Config == nil {
		writeProblem(w, http.StatusBadRequest, "empty_schedule_update", "at least one schedule field is required")
		return
	}
	item, err := api.service.Update(r.Context(), id, schedules.UpdateInput{
		CronExpression: request.CronExpression, Timezone: request.Timezone, Enabled: request.Enabled, Config: request.Config,
	}, time.Now().UTC())
	if err != nil {
		writeScheduleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "schedule_id": item.ID, "schedule": item,
		"blockers": []any{}, "next_actions": []any{},
		"safety": map[string]any{"submits_candidates": false, "uploads_torrents": false},
	})
}

func (api scheduleAPI) notifications(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 25, 1, 100)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	items, err := api.service.ListNotifications(r.Context(), limit)
	if err != nil {
		writeScheduleError(w, err)
		return
	}
	for index := range items {
		items[index].Payload = redactJSON(items[index].Payload)
		items[index].RemoteReceipt = redactJSON(items[index].RemoteReceipt)
	}
	blockers := []map[string]any{}
	nextActions := []map[string]any{}
	for _, item := range items {
		if item.Status == "outcome_unknown" {
			blockers = append(blockers, map[string]any{
				"code": "notification_delivery_outcome_unknown", "message": "Discord delivery must be reconciled before any retry",
				"notification_id": item.ID, "channel": item.Channel,
			})
			nextActions = append(nextActions, map[string]any{
				"action": "reconcile_notification", "parameters": map[string]any{"notification_id": item.ID},
			})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "count": len(items), "notifications": items,
		"blockers": blockers, "next_actions": nextActions,
	})
}

func (api scheduleAPI) reconcileNotification(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	id := r.PathValue("notification_id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_notification_id", "notification_id must be a UUID")
		return
	}
	var request schedules.NotificationReconciliationInput
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	request.ActorID = principal.UserID
	item, err := api.service.ReconcileNotification(r.Context(), id, request, time.Now().UTC())
	if err != nil {
		switch {
		case errors.Is(err, schedules.ErrNotFound):
			writeProblem(w, http.StatusNotFound, "notification_not_found", "notification was not found")
		case errors.Is(err, schedules.ErrConflict):
			writeProblem(w, http.StatusConflict, "notification_reconciliation_conflict", "only an outcome_unknown notification can be reconciled")
		case errors.Is(err, schedules.ErrInvalid):
			writeProblem(w, http.StatusBadRequest, "invalid_notification_reconciliation", err.Error())
		default:
			writeProblem(w, http.StatusInternalServerError, "internal_error", "notification reconciliation could not be completed")
		}
		return
	}
	item.Payload = redactJSON(item.Payload)
	item.RemoteReceipt = redactJSON(item.RemoteReceipt)
	nextActions := []any{}
	if item.Status == "queued" {
		nextActions = []any{map[string]any{"action": "wait_for_notification_delivery", "parameters": map[string]any{"notification_id": item.ID}}}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": item.Status, "notification_id": item.ID, "notification": item,
		"blockers": []any{}, "next_actions": nextActions,
	})
}

func writeScheduleError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, schedules.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "schedule_not_found", "daily candidate schedule was not found")
	case errors.Is(err, schedules.ErrConflict):
		writeProblem(w, http.StatusConflict, "schedule_conflict", "a daily candidate schedule with this name already exists")
	case errors.Is(err, schedules.ErrInvalid):
		writeProblem(w, http.StatusBadRequest, "invalid_schedule", err.Error())
	default:
		writeProblem(w, http.StatusInternalServerError, "internal_error", "schedule request could not be completed")
	}
}
