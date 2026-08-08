package server

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/auditlog"
)

type AuditLogService interface {
	List(context.Context, auditlog.Filter) (auditlog.Page, error)
}

type auditLogAPI struct{ service AuditLogService }

func registerAuditLogRoutes(mux *http.ServeMux, service AuditLogService) {
	api := auditLogAPI{service: service}
	mux.HandleFunc("GET /api/v2/audit-events", api.list)
}

func (api auditLogAPI) list(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "audit:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 50, 1, 100)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	filter := auditlog.Filter{
		ActorType: strings.TrimSpace(r.URL.Query().Get("actor_type")), Action: strings.TrimSpace(r.URL.Query().Get("action")),
		ResourceType: strings.TrimSpace(r.URL.Query().Get("resource_type")), ResourceID: strings.TrimSpace(r.URL.Query().Get("resource_id")),
		Limit: limit,
	}
	if cursor := strings.TrimSpace(r.URL.Query().Get("cursor")); cursor != "" {
		createdAt, id, err := decodeJobCursor(cursor)
		if err != nil {
			writeProblem(w, http.StatusBadRequest, "invalid_cursor", "audit cursor is invalid or malformed")
			return
		}
		filter.BeforeCreatedAt, filter.BeforeID = &createdAt, id
	}
	page, err := api.service.List(r.Context(), filter)
	if err != nil {
		if errors.Is(err, auditlog.ErrInvalid) {
			writeProblem(w, http.StatusBadRequest, "invalid_audit_filter", err.Error())
			return
		}
		writeProblem(w, http.StatusInternalServerError, "internal_error", "audit events could not be listed")
		return
	}
	for index := range page.Events {
		page.Events[index].Payload = redactJSON(page.Events[index].Payload)
	}
	nextCursor := ""
	if page.HasMore && len(page.Events) > 0 {
		last := page.Events[len(page.Events)-1]
		nextCursor = encodeAuditCursor(last.CreatedAt, last.ID)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "audit_events": page.Events,
		"has_more": page.HasMore, "next_cursor": nextCursor,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func encodeAuditCursor(createdAt time.Time, id string) string {
	body, _ := json.Marshal(jobCursor{CreatedAt: createdAt.UTC(), ID: id})
	return base64.RawURLEncoding.EncodeToString(body)
}
