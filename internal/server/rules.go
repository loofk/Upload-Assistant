package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type RuleService interface {
	Import(context.Context, []byte, workflow.Actor) (rules.Revision, error)
	Approve(context.Context, string, string, string, workflow.Actor) (rules.Revision, error)
	Activate(context.Context, string, workflow.Actor) (rules.Revision, error)
	Active(context.Context, string) (rules.Revision, error)
	Get(context.Context, string) (rules.Revision, error)
	List(context.Context, string) ([]rules.Revision, error)
	ListSites(context.Context) ([]rules.SiteSummary, error)
	ReadMarkdown(rules.Revision) ([]byte, error)
}

type rulesAPI struct {
	service RuleService
}

type importRuleRequest struct {
	Markdown string `json:"markdown"`
}

type approveRuleRequest struct {
	Fingerprint string `json:"fingerprint"`
	Comment     string `json:"comment,omitempty"`
}

func registerRuleRoutes(mux *http.ServeMux, service RuleService) {
	api := rulesAPI{service: service}
	mux.HandleFunc("GET /api/v2/sites", api.sites)
	mux.HandleFunc("GET /api/v2/sites/{site_code}/rules", api.list)
	mux.HandleFunc("GET /api/v2/sites/{site_code}/rules/active", api.active)
	mux.HandleFunc("POST /api/v2/site-rules/import", api.importRule)
	mux.HandleFunc("GET /api/v2/site-rules/{revision_id}", api.get)
	mux.HandleFunc("GET /api/v2/site-rules/{revision_id}/markdown", api.markdown)
	mux.HandleFunc("POST /api/v2/site-rules/{revision_id}/approve", api.approve)
	mux.HandleFunc("POST /api/v2/site-rules/{revision_id}/activate", api.activate)
}

func (a rulesAPI) sites(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	sites, err := a.service.ListSites(r.Context())
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "ready", "sites": sites, "blockers": []any{}, "next_actions": []any{}})
}

func (a rulesAPI) list(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	code := strings.ToUpper(strings.TrimSpace(r.PathValue("site_code")))
	revisions, err := a.service.List(r.Context(), code)
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "site_code": code, "revisions": revisions,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (a rulesAPI) active(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	code := strings.ToUpper(strings.TrimSpace(r.PathValue("site_code")))
	revision, err := a.service.Active(r.Context(), code)
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ruleEnvelope(revision))
}

func (a rulesAPI) importRule(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request importRuleRequest
	// A JSON string can almost double normal Markdown through escaped newlines,
	// quotes, and backslashes. ParseMarkdown applies the authoritative decoded
	// 8 MiB limit after this bounded transport envelope is decoded.
	if err := decodeJSONLimit(w, r, &request, 2*rules.MaxMarkdownBytes+(1<<20)); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if strings.TrimSpace(request.Markdown) == "" {
		writeProblem(w, http.StatusBadRequest, "rule_markdown_required", "markdown is required")
		return
	}
	revision, err := a.service.Import(r.Context(), []byte(request.Markdown), workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, ruleEnvelope(revision))
}

func (a rulesAPI) get(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	id, ok := ruleRevisionID(w, r)
	if !ok {
		return
	}
	revision, err := a.service.Get(r.Context(), id)
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ruleEnvelope(revision))
}

func (a rulesAPI) markdown(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	id, ok := ruleRevisionID(w, r)
	if !ok {
		return
	}
	revision, err := a.service.Get(r.Context(), id)
	if err != nil {
		writeRuleError(w, err)
		return
	}
	markdown, err := a.service.ReadMarkdown(revision)
	if err != nil {
		writeRuleError(w, err)
		return
	}
	w.Header().Set("Content-Type", "text/markdown; charset=utf-8")
	w.Header().Set("ETag", `"`+revision.MarkdownSHA256+`"`)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(markdown)
}

func (a rulesAPI) approve(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	id, ok := ruleRevisionID(w, r)
	if !ok {
		return
	}
	var request approveRuleRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	revision, err := a.service.Approve(
		r.Context(), id, request.Fingerprint, request.Comment,
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ruleEnvelope(revision))
}

func (a rulesAPI) activate(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	id, ok := ruleRevisionID(w, r)
	if !ok {
		return
	}
	revision, err := a.service.Activate(r.Context(), id, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeRuleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ruleEnvelope(revision))
}

func ruleEnvelope(revision rules.Revision) map[string]any {
	blockers := make([]map[string]string, 0)
	nextActions := make([]map[string]string, 0)
	if revision.Status == "draft" {
		blockers = append(blockers, map[string]string{"code": "rule_revision_not_approved", "message": "rule revision is not approved"})
		nextActions = append(nextActions, map[string]string{"action": "review_rule_revision", "revision_id": revision.ID})
	}
	return map[string]any{
		"ok": true, "status": revision.Status, "site_code": revision.SiteCode,
		"rule_revision_id": revision.ID, "fingerprint": revision.Fingerprint,
		"revision": revision, "blockers": blockers, "next_actions": nextActions,
	}
}

func ruleRevisionID(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("revision_id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_rule_revision_id", "revision_id must be a UUID")
		return "", false
	}
	return id, true
}

func writeRuleError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, rules.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "rule_not_found", err.Error())
	case errors.Is(err, rules.ErrSourceIncomplete):
		writeProblem(w, http.StatusConflict, "rule_source_incomplete", err.Error())
	case errors.Is(err, rules.ErrConflict):
		writeProblem(w, http.StatusConflict, "rule_conflict", err.Error())
	default:
		writeProblem(w, http.StatusBadRequest, "invalid_rule_document", err.Error())
	}
}
