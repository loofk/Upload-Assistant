package server

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

type TokenLifecycleService interface {
	ListTokens(context.Context, security.Principal) ([]security.TokenRecord, error)
	CreateToken(context.Context, security.Principal, security.CreateTokenInput, string) (security.CreatedToken, error)
	RevokeToken(context.Context, security.Principal, string, string) (security.TokenRecord, error)
}

type operationsAPI struct {
	store                                      *operations.Store
	diagnostics                                *operations.DiagnosticService
	ruleAnalyses                               *ruleAnalysisCoordinator
	backups                                    *operations.BackupManager
	tokens                                     TokenLifecycleService
	dataDir, downloadsDir, backupsDir, version string
	dropped                                    func() uint64
}

func registerOperationsRoutes(mux *http.ServeMux, api operationsAPI) {
	mux.HandleFunc("GET /api/v2/operational-logs", api.listLogs)
	mux.HandleFunc("GET /api/v2/operational-logs/{log_id}/context", api.getLogContext)
	mux.HandleFunc("GET /api/v2/operational-logs/stream", api.streamLogs)
	mux.HandleFunc("GET /api/v2/operational-logs/export", api.exportLogs)
	mux.HandleFunc("GET /api/v2/incidents", api.listIncidents)
	mux.HandleFunc("GET /api/v2/incidents/{incident_id}", api.getIncident)
	mux.HandleFunc("POST /api/v2/incidents/{incident_id}/acknowledge", api.acknowledgeIncident)
	mux.HandleFunc("POST /api/v2/incidents/{incident_id}/resolve", api.resolveIncident)
	mux.HandleFunc("GET /api/v2/llm-providers", api.listProviders)
	mux.HandleFunc("PUT /api/v2/llm-providers/{provider_id}", api.putProvider)
	mux.HandleFunc("POST /api/v2/llm-providers/{provider_id}/probe", api.probeProvider)
	mux.HandleFunc("POST /api/v2/site-rules/analyze", api.analyzeSiteRules)
	mux.HandleFunc("POST /api/v2/site-rules/analyze/stream", api.analyzeSiteRulesStream)
	mux.HandleFunc("GET /api/v2/site-rules/analyze/result", api.getSiteRuleAnalysisResult)
	mux.HandleFunc("POST /api/v2/diagnostics", api.createDiagnostic)
	mux.HandleFunc("GET /api/v2/diagnostics", api.listDiagnostics)
	mux.HandleFunc("GET /api/v2/diagnostics/{diagnostic_id}", api.getDiagnostic)
	mux.HandleFunc("POST /api/v2/diagnostics/{diagnostic_id}/messages", api.addDiagnosticMessage)
	mux.HandleFunc("GET /api/v2/operations/overview", api.overview)
	mux.HandleFunc("GET /api/v2/operations/settings", api.getSettings)
	mux.HandleFunc("PUT /api/v2/operations/settings", api.putSettings)
	mux.HandleFunc("GET /api/v2/api-tokens", api.listTokens)
	mux.HandleFunc("POST /api/v2/api-tokens", api.createToken)
	mux.HandleFunc("DELETE /api/v2/api-tokens/{token_id}", api.revokeToken)
	mux.HandleFunc("GET /api/v2/backups/policy", api.getBackupPolicy)
	mux.HandleFunc("PUT /api/v2/backups/policy", api.putBackupPolicy)
	mux.HandleFunc("GET /api/v2/backups/runs", api.listBackups)
	mux.HandleFunc("POST /api/v2/backups", api.createBackup)
	mux.HandleFunc("POST /api/v2/backups/{backup_id}/verify", api.verifyBackup)
}

func (api operationsAPI) getLogContext(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "logs:read"); !ok {
		return
	}
	id, err := strconv.ParseInt(strings.TrimSpace(r.PathValue("log_id")), 10, 64)
	if err != nil || id < 1 {
		writeProblem(w, http.StatusBadRequest, "invalid_log_id", "log_id must be a positive integer")
		return
	}
	context, err := api.store.GetLogContext(r.Context(), id)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "log_id": id, "context": context,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func parseLogFilter(r *http.Request, max int) (operations.LogFilter, error) {
	limit, err := parseIntQuery(r, "limit", 100, 1, max)
	if err != nil {
		return operations.LogFilter{}, err
	}
	statusCode, err := parseIntQuery(r, "status_code", 0, 0, 599)
	if err != nil {
		return operations.LogFilter{}, err
	}
	if statusCode != 0 && statusCode < 100 {
		return operations.LogFilter{}, errors.New("status_code must be between 100 and 599")
	}
	filter := operations.LogFilter{Component: strings.TrimSpace(r.URL.Query().Get("component")), Keyword: strings.TrimSpace(r.URL.Query().Get("q")), ErrorCode: strings.TrimSpace(r.URL.Query().Get("error_code")), RequestID: strings.TrimSpace(r.URL.Query().Get("request_id")), TraceID: strings.TrimSpace(r.URL.Query().Get("trace_id")), JobID: strings.TrimSpace(r.URL.Query().Get("job_id")), AttemptID: strings.TrimSpace(r.URL.Query().Get("attempt_id")), StatusCode: statusCode, Limit: limit}
	if levels := strings.TrimSpace(r.URL.Query().Get("level")); levels != "" {
		filter.Levels = strings.Split(levels, ",")
	}
	for name, target := range map[string]**time.Time{"from": &filter.From, "to": &filter.To} {
		if value := strings.TrimSpace(r.URL.Query().Get(name)); value != "" {
			parsed, e := time.Parse(time.RFC3339, value)
			if e != nil {
				return filter, fmt.Errorf("%s must be RFC3339", name)
			}
			*target = &parsed
		}
	}
	if cursor := strings.TrimSpace(r.URL.Query().Get("cursor")); cursor != "" {
		var occurredAt *time.Time
		occurredAt, filter.BeforeID, err = decodeOperationalLogCursor(cursor)
		filter.BeforeOccurredAt = occurredAt
		if err != nil {
			return filter, errors.New("invalid log cursor")
		}
	}
	return filter, nil
}

type operationalLogCursor struct {
	OccurredAt time.Time `json:"occurred_at"`
	ID         int64     `json:"id"`
}

func encodeOperationalLogCursor(occurredAt time.Time, id int64) string {
	body, _ := json.Marshal(operationalLogCursor{OccurredAt: occurredAt.UTC(), ID: id})
	return base64.RawURLEncoding.EncodeToString(body)
}

func decodeOperationalLogCursor(value string) (*time.Time, int64, error) {
	body, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || len(body) > 512 {
		return nil, 0, errors.New("invalid log cursor encoding")
	}
	// Accept the v1 ID-only cursor so existing bookmarked console URLs continue
	// to work after the stable (occurred_at,id) cursor rollout.
	if id, legacyErr := strconv.ParseInt(string(body), 10, 64); legacyErr == nil && id > 0 {
		return nil, id, nil
	}
	var cursor operationalLogCursor
	if err = json.Unmarshal(body, &cursor); err != nil || cursor.ID < 1 || cursor.OccurredAt.IsZero() {
		return nil, 0, errors.New("invalid log cursor value")
	}
	at := cursor.OccurredAt.UTC()
	return &at, cursor.ID, nil
}

func (api operationsAPI) listLogs(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "logs:read"); !ok {
		return
	}
	filter, err := parseLogFilter(r, 500)
	if err != nil {
		writeProblem(w, 400, "invalid_log_filter", err.Error())
		return
	}
	page, err := api.store.ListLogSummaries(r.Context(), filter)
	if err != nil {
		writeProblem(w, 400, "invalid_log_filter", err.Error())
		return
	}
	cursor := ""
	if page.HasMore && len(page.Logs) > 0 {
		last := page.Logs[len(page.Logs)-1]
		cursor = encodeOperationalLogCursor(last.OccurredAt, last.ID)
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "operational_logs": page.Logs, "has_more": page.HasMore, "next_cursor": cursor, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) exportLogs(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "logs:export"); !ok {
		return
	}
	filter, err := parseLogFilter(r, 10000)
	if err != nil {
		writeProblem(w, 400, "invalid_log_filter", err.Error())
		return
	}
	if r.URL.Query().Get("limit") == "" {
		filter.Limit = 10000
	}
	page, err := api.store.ListLogs(r.Context(), filter)
	if err != nil {
		writeProblem(w, 400, "invalid_log_filter", err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.Header().Set("Content-Disposition", `attachment; filename="upload-assistant-operational-logs.ndjson"`)
	encoder := json.NewEncoder(w)
	for _, entry := range page.Logs {
		if encoder.Encode(entry) != nil {
			return
		}
	}
}
func (api operationsAPI) streamLogs(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "logs:read"); !ok {
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeProblem(w, 500, "stream_unavailable", "SSE is unavailable")
		return
	}
	filter, err := parseLogFilter(r, 200)
	if err != nil {
		writeProblem(w, 400, "invalid_log_filter", err.Error())
		return
	}
	after := filter.AfterID
	if value := strings.TrimSpace(r.Header.Get("Last-Event-ID")); value != "" {
		after, err = strconv.ParseInt(value, 10, 64)
		if err != nil || after < 0 {
			writeProblem(w, 400, "invalid_last_event_id", "Last-Event-ID must be a non-negative log ID")
			return
		}
	}
	filter.BeforeID = 0
	filter.BeforeOccurredAt = nil
	filter.AfterID = after
	filter.Limit = 200
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	for {
		filter.AfterID = after
		page, err := api.store.ListLogSummaries(r.Context(), filter)
		if err != nil {
			return
		}
		for _, entry := range page.Logs {
			body, _ := json.Marshal(entry)
			if _, err = fmt.Fprintf(w, "id: %d\nevent: operational-log\ndata: %s\n\n", entry.ID, body); err != nil {
				return
			}
			after = entry.ID
		}
		if len(page.Logs) > 0 {
			flusher.Flush()
		}
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.C:
			_, _ = fmt.Fprint(w, ": keepalive\n\n")
			flusher.Flush()
		case <-ticker.C:
		}
	}
}

func (api operationsAPI) listIncidents(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "operations:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 50, 1, 200)
	if err != nil {
		writeProblem(w, 400, "invalid_limit", err.Error())
		return
	}
	filter := operations.IncidentFilter{Status: r.URL.Query().Get("status"), Severity: r.URL.Query().Get("severity"), Kind: r.URL.Query().Get("kind"), JobID: r.URL.Query().Get("job_id"), Limit: limit}
	if cursor := r.URL.Query().Get("cursor"); cursor != "" {
		at, id, e := decodeJobCursor(cursor)
		if e != nil {
			writeProblem(w, 400, "invalid_cursor", "incident cursor is invalid")
			return
		}
		filter.Before = &at
		filter.BeforeID = id
	}
	page, err := api.store.ListIncidents(r.Context(), filter)
	if err != nil {
		writeProblem(w, 400, "invalid_incident_filter", err.Error())
		return
	}
	cursor := ""
	if page.HasMore && len(page.Incidents) > 0 {
		last := page.Incidents[len(page.Incidents)-1]
		cursor = encodeAuditCursor(last.LastOccurredAt, last.ID)
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "incidents": page.Incidents, "has_more": page.HasMore, "next_cursor": cursor, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) getIncident(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "operations:read"); !ok {
		return
	}
	item, err := api.store.GetIncident(r.Context(), r.PathValue("incident_id"))
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": item.Status, "incident_id": item.ID, "incident": item, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) acknowledgeIncident(w http.ResponseWriter, r *http.Request) {
	api.changeIncident(w, r, "acknowledged")
}
func (api operationsAPI) resolveIncident(w http.ResponseWriter, r *http.Request) {
	api.changeIncident(w, r, "resolved")
}
func (api operationsAPI) changeIncident(w http.ResponseWriter, r *http.Request, status string) {
	principal, ok := requireScope(w, r, "operations:manage")
	if !ok {
		return
	}
	item, err := api.store.SetIncidentStatus(r.Context(), r.PathValue("incident_id"), status, principal, operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": item.Status, "incident_id": item.ID, "incident": item, "blockers": []any{}, "next_actions": []any{}})
}

func (api operationsAPI) listProviders(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "llm:manage"); !ok {
		return
	}
	items, err := api.store.ListProviders(r.Context())
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "llm_providers": items, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) putProvider(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "llm:manage")
	if !ok {
		return
	}
	var input struct {
		Name             string   `json:"name"`
		BaseURL          string   `json:"base_url"`
		Model            string   `json:"model"`
		DataLevel        string   `json:"data_level"`
		APIMode          string   `json:"api_mode"`
		ReasoningEffort  string   `json:"reasoning_effort"`
		UseCases         []string `json:"use_cases"`
		JSONMode         bool     `json:"json_mode"`
		StreamingEnabled bool     `json:"streaming_enabled"`
		TimeoutSeconds   int      `json:"timeout_seconds"`
		Enabled          bool     `json:"enabled"`
		OutboundConsent  bool     `json:"outbound_consent"`
		APIKey           string   `json:"api_key"`
	}
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, 400, "invalid_request", err.Error())
		return
	}
	item, err := api.store.PutProvider(r.Context(), r.PathValue("provider_id"), operations.ProviderInput{Name: input.Name, BaseURL: input.BaseURL, Model: input.Model, DataLevel: input.DataLevel, APIMode: input.APIMode, ReasoningEffort: input.ReasoningEffort, UseCases: input.UseCases, JSONMode: input.JSONMode, StreamingEnabled: input.StreamingEnabled, TimeoutSeconds: input.TimeoutSeconds, Enabled: input.Enabled, OutboundConsent: input.OutboundConsent, APIKey: input.APIKey}, principal, api.diagnostics.Secrets, operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "provider_id": item.ID, "llm_provider": item, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) probeProvider(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "llm:manage"); !ok {
		return
	}
	result, err := api.diagnostics.Probe(r.Context(), r.PathValue("provider_id"), r.URL.Query().Get("stage"))
	if err != nil {
		if errors.Is(err, operations.ErrInvalid) || errors.Is(err, operations.ErrNotFound) {
			writeOpsError(w, err)
			return
		}
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"ok": false, "status": "failed", "provider_id": r.PathValue("provider_id"), "probe": result,
			"blockers":     []map[string]string{{"code": result.Evidence.ErrorCode, "message": err.Error()}},
			"next_actions": []map[string]string{{"action": "inspect_provider_probe_evidence"}},
		})
		return
	}
	provider, _ := api.store.GetProvider(r.Context(), r.PathValue("provider_id"))
	writeJSON(w, 200, map[string]any{"ok": true, "status": result.Status, "provider_id": r.PathValue("provider_id"), "probe": result, "llm_provider": provider, "blockers": []any{}, "next_actions": []any{}})
}

func (api operationsAPI) analyzeSiteRules(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var input struct {
		ProviderID       string   `json:"provider_id"`
		SourceRevisionID string   `json:"source_revision_id"`
		SiteCode         string   `json:"site_code"`
		DisplayName      string   `json:"display_name"`
		Roles            []string `json:"roles"`
		SourceURL        string   `json:"source_url"`
		SourceScope      string   `json:"source_scope"`
		SourceComplete   bool     `json:"source_complete"`
		SourceText       string   `json:"source_text"`
	}
	if err := decodeJSONLimit(w, r, &input, 2*operations.MaxRuleAnalysisBytes+(1<<20)); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	result, err := api.diagnostics.AnalyzeRuleText(r.Context(), operations.RuleAnalysisInput{
		ProviderID: input.ProviderID, SourceRevisionID: input.SourceRevisionID,
		SiteCode: input.SiteCode, DisplayName: input.DisplayName,
		Roles: input.Roles, SourceURL: input.SourceURL, SourceScope: input.SourceScope,
		SourceComplete: input.SourceComplete, SourceText: input.SourceText,
	}, principal, operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		if errors.Is(err, operations.ErrInvalid) || errors.Is(err, operations.ErrConflict) || errors.Is(err, operations.ErrNotFound) {
			writeOpsError(w, err)
		} else if failure, ok := operations.DescribeProviderCallFailure(err); ok {
			status := providerFailureHTTPStatus(failure.Code)
			if status == http.StatusServiceUnavailable {
				w.Header().Set("Retry-After", "5")
			}
			writeProblemWithAttributes(w, status, failure.Code, failure.Detail, map[string]any{
				"operation": "site_rule_analysis", "provider_id": failure.Evidence.ProviderID,
				"model": failure.Evidence.Model, "api_mode": failure.Evidence.APIMode,
				"reasoning_effort": failure.Evidence.ReasoningEffort,
				"endpoint_path":    failure.Evidence.EndpointPath, "timeout_seconds": failure.Evidence.TimeoutSeconds,
				"upstream_status_code": failure.Evidence.StatusCode, "content_type": failure.Evidence.ContentType,
				"response_sha256": failure.Evidence.ResponseSHA256, "response_shape": failure.Evidence.ResponseShape,
				"stream_error": failure.Evidence.StreamError, "attempt": failure.Evidence.Attempt,
				"max_attempts":            failure.Evidence.MaxAttempts,
				"provider_latency_ms":     failure.Evidence.LatencyMS,
				"external_call_performed": failure.Evidence.ExternalCallPerformed,
			}, providerFailureNextActions(failure))
		} else {
			writeProblem(w, http.StatusBadGateway, "rule_analysis_failed", "the provider could not produce a valid rule draft")
		}
		return
	}
	blockers := []any{}
	if !result.SourceComplete {
		blockers = append(blockers, map[string]any{"code": "rule_source_incomplete", "message": "原文尚未标记为完整，草稿不可审批或激活"})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "draft_ready", "analysis": result, "blockers": blockers,
		"next_actions": []map[string]any{{"action": "review_ai_rule_draft"}, {"action": "import_site_rule_markdown"}},
	})
}

func providerFailureHTTPStatus(code string) int {
	switch code {
	case "provider_timeout":
		return http.StatusGatewayTimeout
	case "provider_busy":
		return http.StatusServiceUnavailable
	case "provider_configuration_changed":
		return http.StatusConflict
	default:
		return http.StatusBadGateway
	}
}

func providerFailureNextActions(failure operations.ProviderCallFailure) []map[string]string {
	switch failure.Code {
	case "provider_timeout":
		return []map[string]string{{"action": "increase_provider_timeout"}, {"action": "reduce_reasoning_effort"}, {"action": "retry_rule_analysis"}}
	case "provider_http_error":
		switch failure.Evidence.StatusCode {
		case http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout, 520, 521, 522, 523, 524:
			return []map[string]string{{"action": "inspect_upstream_gateway_and_origin_latency"}, {"action": "reduce_reasoning_effort_or_change_model"}}
		}
		return []map[string]string{{"action": "inspect_provider_credentials_and_upstream_status"}}
	case "provider_stream_error":
		return []map[string]string{{"action": "inspect_operational_log_provider_stream_error"}, {"action": "reduce_reasoning_effort_or_change_model"}}
	case "provider_schema_invalid":
		return []map[string]string{{"action": "verify_provider_api_mode"}}
	case "provider_output_truncated", "provider_output_incomplete":
		return []map[string]string{{"action": "inspect_provider_completion_status"}, {"action": "use_model_with_larger_output_budget"}}
	case "provider_configuration_changed":
		return []map[string]string{{"action": "start_new_analysis_with_current_provider_configuration"}}
	case "provider_busy":
		return []map[string]string{{"action": "retry_after_current_analyses_finish"}}
	default:
		return []map[string]string{{"action": "inspect_operational_log_provider_evidence"}}
	}
}

func (api operationsAPI) createDiagnostic(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "diagnostics:run")
	if !ok {
		return
	}
	var input struct {
		ProviderID string `json:"provider_id"`
		JobID      string `json:"job_id"`
		IncidentID string `json:"incident_id"`
		LogID      int64  `json:"log_id"`
	}
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, 400, "invalid_request", err.Error())
		return
	}
	item, err := api.store.CreateDiagnosticForTarget(r.Context(), input.ProviderID, input.JobID, input.IncidentID, input.LogID, principal)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 202, diagnosticEnvelope(item))
}
func (api operationsAPI) listDiagnostics(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "diagnostics:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 50, 1, 100)
	if err != nil {
		writeProblem(w, 400, "invalid_limit", err.Error())
		return
	}
	items, err := api.store.ListDiagnostics(r.Context(), limit)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "diagnostics": items, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) getDiagnostic(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "diagnostics:read"); !ok {
		return
	}
	item, err := api.store.GetDiagnostic(r.Context(), r.PathValue("diagnostic_id"))
	if err != nil {
		writeOpsError(w, err)
		return
	}
	envelope := diagnosticEnvelope(item)
	messages, _ := api.store.ListDiagnosticMessages(r.Context(), item.ID)
	envelope["messages"] = messages
	writeJSON(w, 200, envelope)
}
func (api operationsAPI) addDiagnosticMessage(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "diagnostics:run")
	if !ok {
		return
	}
	var input struct {
		Question string `json:"question"`
	}
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, 400, "invalid_request", err.Error())
		return
	}
	sequence, err := api.store.AddDiagnosticMessage(r.Context(), r.PathValue("diagnostic_id"), input.Question, principal)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 202, map[string]any{"ok": true, "status": "queued", "diagnostic_id": r.PathValue("diagnostic_id"), "message_sequence": sequence, "blockers": []any{}, "next_actions": []any{}})
}
func diagnosticEnvelope(item operations.Diagnostic) map[string]any {
	return map[string]any{"ok": item.Status == "complete" || item.Status == "queued" || item.Status == "running", "status": item.Status, "diagnostic_id": item.ID, "job_id": item.JobID, "incident_id": item.IncidentID, "diagnostic": item, "summary": func() string {
		if item.Result != nil {
			return item.Result.Summary
		}
		return ""
	}(), "blockers": func() any {
		if item.Status == "failed" {
			return []map[string]string{{"code": item.ErrorCode, "message": item.ErrorMessage}}
		}
		return []any{}
	}(), "next_actions": []any{}}
}

func (api operationsAPI) overview(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "operations:read"); !ok {
		return
	}
	dropped := uint64(0)
	if api.dropped != nil {
		dropped = api.dropped()
	}
	value, err := api.store.Overview(r.Context(), api.dataDir, api.downloadsDir, api.backupsDir, api.version, dropped)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "overview": value, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) getSettings(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "operations:read"); !ok {
		return
	}
	value, err := api.store.GetSettings(r.Context())
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "settings": value, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) putSettings(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "operations:manage")
	if !ok {
		return
	}
	var input operations.Settings
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, 400, "invalid_request", err.Error())
		return
	}
	value, err := api.store.PutSettings(r.Context(), input, principal, operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "settings": value, "blockers": []any{}, "next_actions": []any{}})
}

func (api operationsAPI) listTokens(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "tokens:manage")
	if !ok {
		return
	}
	items, err := api.tokens.ListTokens(r.Context(), principal)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "api_tokens": items, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) createToken(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "tokens:manage")
	if !ok {
		return
	}
	var input struct {
		Name          string   `json:"name"`
		Scopes        []string `json:"scopes"`
		ExpiresInDays int      `json:"expires_in_days"`
	}
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, 400, "invalid_request", err.Error())
		return
	}
	item, err := api.tokens.CreateToken(r.Context(), principal, security.CreateTokenInput{Name: input.Name, Scopes: input.Scopes, ExpiresInDays: input.ExpiresInDays}, operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 201, map[string]any{"ok": true, "status": "complete", "token_id": item.ID, "api_token": item, "summary": "API token is shown once; store it in a password manager or a mode-0600 file", "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) revokeToken(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "tokens:manage")
	if !ok {
		return
	}
	item, err := api.tokens.RevokeToken(r.Context(), principal, r.PathValue("token_id"), operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "revoked", "token_id": item.ID, "api_token": item, "blockers": []any{}, "next_actions": []any{}})
}

func (api operationsAPI) getBackupPolicy(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "backups:read"); !ok {
		return
	}
	p, err := api.store.GetBackupPolicy(r.Context())
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "policy": p, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) putBackupPolicy(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "backups:manage")
	if !ok {
		return
	}
	var input struct {
		Enabled          bool   `json:"enabled"`
		Recipient        string `json:"recipient"`
		Schedule         string `json:"schedule"`
		RetentionCount   int    `json:"retention_count"`
		GenerateIdentity bool   `json:"generate_identity"`
	}
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, 400, "invalid_request", err.Error())
		return
	}
	identity := ""
	if input.GenerateIdentity {
		var err error
		identity, input.Recipient, err = api.backups.GenerateIdentity(r.Context())
		if err != nil {
			writeOpsError(w, err)
			return
		}
	}
	p, err := api.store.PutBackupPolicy(r.Context(), operations.BackupPolicy{Enabled: input.Enabled, Recipient: input.Recipient, Schedule: input.Schedule, RetentionCount: input.RetentionCount}, principal, operations.CorrelationFromContext(r.Context()).TraceID)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	response := map[string]any{"ok": true, "status": "ready", "policy": p, "blockers": []any{}, "next_actions": []any{}}
	if identity != "" {
		response["identity_once"] = identity
		response["summary"] = "Private age identity is shown once and must be stored offline"
	}
	writeJSON(w, 200, response)
}
func (api operationsAPI) listBackups(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "backups:read"); !ok {
		return
	}
	items, err := api.store.ListBackups(r.Context(), 50)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": "ready", "backup_runs": items, "blockers": []any{}, "next_actions": []any{}})
}
func (api operationsAPI) createBackup(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "backups:manage")
	if !ok {
		return
	}
	item, err := api.backups.Create(r.Context(), principal)
	if err != nil {
		writeOpsError(w, err)
		return
	}
	code := 201
	if item.Status == "deferred" {
		code = 202
	}
	writeJSON(w, code, map[string]any{"ok": item.Status != "failed", "status": item.Status, "backup_id": item.ID, "backup": item, "blockers": func() any {
		if item.Status == "deferred" {
			return []map[string]string{{"code": "active_write_jobs", "message": "backup delayed until jobs reach a safe boundary"}}
		}
		return []any{}
	}(), "next_actions": []any{}})
}
func (api operationsAPI) verifyBackup(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "backups:manage"); !ok {
		return
	}
	item, err := api.backups.Verify(r.Context(), r.PathValue("backup_id"))
	if err != nil {
		writeOpsError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "status": item.Status, "backup_id": item.ID, "backup": item, "blockers": []any{}, "next_actions": []any{}})
}

func writeOpsError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, operations.ErrNotFound):
		writeProblem(w, 404, "not_found", "operations resource was not found")
	case errors.Is(err, operations.ErrConflict), errors.Is(err, security.ErrForbidden):
		writeProblem(w, 409, "state_conflict", err.Error())
	case errors.Is(err, operations.ErrInvalid):
		writeProblem(w, 400, "invalid_request", err.Error())
	default:
		writeProblem(w, 500, "internal_error", "operations request could not be completed")
	}
}
