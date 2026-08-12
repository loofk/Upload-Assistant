package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/rulecollector"
	"github.com/loofk/upload-assistant/v2/internal/siteaccess"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type RuleCollectionService interface {
	GetSourceSet(context.Context, string) (rulecollector.SourceSet, error)
	PutSourceSet(context.Context, string, rulecollector.SourceSetInput, workflow.Actor) (rulecollector.SourceSet, error)
	CreateRun(context.Context, string, rulecollector.CreateRunInput, workflow.Actor) (rulecollector.CollectionRun, error)
	GetRun(context.Context, string) (rulecollector.CollectionRun, error)
	LatestRun(context.Context, string) (rulecollector.CollectionRun, error)
}

type ruleCollectionAPI struct{ service RuleCollectionService }

func registerRuleCollectionRoutes(mux *http.ServeMux, service RuleCollectionService) {
	api := ruleCollectionAPI{service: service}
	mux.HandleFunc("GET /api/v2/sites/{site_code}/rule-sources", api.getSources)
	mux.HandleFunc("PUT /api/v2/sites/{site_code}/rule-sources", api.putSources)
	mux.HandleFunc("POST /api/v2/sites/{site_code}/rule-collection-runs", api.createRun)
	mux.HandleFunc("GET /api/v2/sites/{site_code}/rule-collection-runs/latest", api.latestRun)
	mux.HandleFunc("GET /api/v2/site-rule-collection-runs/{run_id}", api.getRun)
	mux.HandleFunc("GET /api/v2/site-rule-collection-runs/{run_id}/stream", api.streamRun)
}

func (api ruleCollectionAPI) getSources(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	set, err := api.service.GetSourceSet(r.Context(), strings.ToUpper(strings.TrimSpace(r.PathValue("site_code"))))
	if err != nil {
		writeRuleCollectionError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "ready", "source_set": set, "blockers": sourceSetBlockers(set), "next_actions": sourceSetActions(set)})
}

func (api ruleCollectionAPI) putSources(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var input rulecollector.SourceSetInput
	if err := decodeJSONLimit(w, r, &input, 64<<10); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	set, err := api.service.PutSourceSet(r.Context(), strings.ToUpper(strings.TrimSpace(r.PathValue("site_code"))), input, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeRuleCollectionError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "ready", "source_set": set, "blockers": sourceSetBlockers(set), "next_actions": sourceSetActions(set)})
}

func (api ruleCollectionAPI) createRun(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var input struct {
		SourceSetFingerprint string `json:"source_set_fingerprint"`
		ProviderID           string `json:"provider_id"`
		Confirm              bool   `json:"confirm"`
	}
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if !input.Confirm {
		writeProblem(w, http.StatusBadRequest, "confirmation_required", "confirm=true is required for external rule-page reads and inference")
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" {
		writeProblem(w, http.StatusBadRequest, "idempotency_key_required", "Idempotency-Key header is required")
		return
	}
	run, err := api.service.CreateRun(r.Context(), strings.ToUpper(strings.TrimSpace(r.PathValue("site_code"))), rulecollector.CreateRunInput{
		SourceSetFingerprint: input.SourceSetFingerprint, ProviderID: input.ProviderID,
		Confirm:        input.Confirm,
		IdempotencyKey: idempotencyKey, TraceID: operations.CorrelationFromContext(r.Context()).TraceID,
	}, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeRuleCollectionError(w, err)
		return
	}
	writeRuleCollectionRun(w, http.StatusAccepted, run)
}

func (api ruleCollectionAPI) getRun(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	run, err := api.service.GetRun(r.Context(), strings.TrimSpace(r.PathValue("run_id")))
	if err != nil {
		writeRuleCollectionError(w, err)
		return
	}
	writeRuleCollectionRun(w, http.StatusOK, run)
}

func (api ruleCollectionAPI) latestRun(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	run, err := api.service.LatestRun(r.Context(), strings.ToUpper(strings.TrimSpace(r.PathValue("site_code"))))
	if errors.Is(err, rulecollector.ErrNotFound) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "status": "empty", "blockers": []any{}, "next_actions": []any{}})
		return
	}
	if err != nil {
		writeRuleCollectionError(w, err)
		return
	}
	writeRuleCollectionRun(w, http.StatusOK, run)
}

func (api ruleCollectionAPI) streamRun(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeProblem(w, http.StatusInternalServerError, "streaming_unavailable", "response streaming is unavailable")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("X-Accel-Buffering", "no")
	controller := http.NewResponseController(w)
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	last := ""
	for {
		// The process-wide write timeout is intentionally short for normal HTTP
		// requests. Refresh only this SSE response's deadline on every heartbeat.
		_ = controller.SetWriteDeadline(time.Now().Add(10 * time.Second))
		run, err := api.service.GetRun(r.Context(), strings.TrimSpace(r.PathValue("run_id")))
		if err != nil {
			body, _ := json.Marshal(map[string]any{"code": "rule_collection_read_failed", "message": "读取采集状态失败"})
			_, _ = fmt.Fprintf(w, "event: error\ndata: %s\n\n", body)
			flusher.Flush()
			return
		}
		body, _ := json.Marshal(map[string]any{"ok": run.Status != "failed", "status": run.Status, "run": run, "blockers": runBlockers(run), "next_actions": runActions(run)})
		current := string(body)
		if current != last {
			_, _ = fmt.Fprintf(w, "event: progress\ndata: %s\n\n", body)
			last = current
		} else {
			_, _ = fmt.Fprint(w, ": heartbeat\n\n")
		}
		flusher.Flush()
		if run.Terminal() {
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
		}
	}
}

func writeRuleCollectionRun(w http.ResponseWriter, statusCode int, run rulecollector.CollectionRun) {
	writeJSON(w, statusCode, map[string]any{
		"ok": run.Status != "failed", "status": run.Status, "run_id": run.ID,
		"rule_revision_id": run.RuleRevisionID, "run": run,
		"blockers": runBlockers(run), "next_actions": runActions(run),
	})
}

func sourceSetBlockers(set rulecollector.SourceSet) []map[string]any {
	result := []map[string]any{}
	if set.CookieRequired && !set.CookieConfigured {
		result = append(result, map[string]any{"code": "site_cookie_required", "message": "请先在配置中心保存并启用 Cookie"})
	}
	if len(set.Sources) == 0 {
		result = append(result, map[string]any{"code": "rule_sources_required", "message": "请添加至少一个规则页面地址"})
	}
	if !set.ScopeConfirmed || (set.CookieRequired && !set.CookieHostsConfirmed) {
		result = append(result, map[string]any{"code": "rule_sources_confirmation_required", "message": "请确认来源完整；使用 Cookie 的页面还需确认发送域名"})
	}
	return result
}

func sourceSetActions(set rulecollector.SourceSet) []map[string]any {
	result := []map[string]any{}
	if set.CookieRequired && !set.CookieConfigured {
		result = append(result, map[string]any{"action": "configure_site_cookie", "site_code": set.SiteCode})
	}
	if len(set.Sources) == 0 || !set.ScopeConfirmed || (set.CookieRequired && !set.CookieHostsConfirmed) {
		result = append(result, map[string]any{"action": "configure_rule_sources", "site_code": set.SiteCode})
	}
	return result
}

func runBlockers(run rulecollector.CollectionRun) []map[string]any {
	if run.Status != "failed" {
		return []map[string]any{}
	}
	return []map[string]any{{"code": run.ErrorCode, "message": run.ErrorDetail}}
}

func runActions(run rulecollector.CollectionRun) []map[string]any {
	if run.Status == "ready" {
		return []map[string]any{{"action": "review_site_rule_hard_gates", "rule_revision_id": run.RuleRevisionID}}
	}
	if run.Status == "failed" {
		return []map[string]any{{"action": "review_collection_failure_and_retry", "run_id": run.ID}}
	}
	return []map[string]any{{"action": "wait_for_rule_collection", "run_id": run.ID, "not_before": run.NotBefore}}
}

func writeRuleCollectionError(w http.ResponseWriter, err error) {
	var denied *siteaccess.DeniedError
	switch {
	case errors.Is(err, rulecollector.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "rule_collection_not_found", "rule collection resource is not configured")
	case errors.Is(err, rulecollector.ErrInvalid):
		writeProblem(w, http.StatusBadRequest, "rule_collection_invalid", err.Error())
	case errors.Is(err, rulecollector.ErrCredential):
		writeProblem(w, http.StatusConflict, "site_cookie_required", "请先在配置中心保存并启用 Cookie")
	case errors.Is(err, rulecollector.ErrConflict):
		writeProblem(w, http.StatusConflict, "rule_collection_conflict", err.Error())
	case errors.As(err, &denied):
		writeProblem(w, http.StatusConflict, denied.Code, denied.Message)
	default:
		writeProblem(w, http.StatusInternalServerError, "rule_collection_failed", "规则采集操作失败")
	}
}
