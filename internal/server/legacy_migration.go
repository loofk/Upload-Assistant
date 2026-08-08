package server

import (
	"context"
	"errors"
	"net/http"
	"strconv"

	"github.com/loofk/upload-assistant/v2/internal/legacy"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type LegacyMigrationService interface {
	Preview(context.Context) (legacy.Preview, error)
	Import(context.Context, legacy.ImportRequest, workflow.Actor) (legacy.ImportRecord, error)
	Get(context.Context, string) (legacy.ImportRecord, error)
	List(context.Context, int) ([]legacy.ImportRecord, error)
}

type legacyMigrationAPI struct{ service LegacyMigrationService }

func registerLegacyMigrationRoutes(mux *http.ServeMux, service LegacyMigrationService) {
	api := legacyMigrationAPI{service: service}
	mux.HandleFunc("GET /api/v2/migrations/legacy/preview", api.preview)
	mux.HandleFunc("GET /api/v2/migrations/legacy", api.list)
	mux.HandleFunc("POST /api/v2/migrations/legacy", api.execute)
	mux.HandleFunc("GET /api/v2/migrations/legacy/{import_id}", api.get)
}

func (api legacyMigrationAPI) preview(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	preview, err := api.service.Preview(r.Context())
	if err != nil {
		writeLegacySourceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, preview)
}

func (api legacyMigrationAPI) execute(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	if !principal.HasScope("downloader:manage") {
		writeProblem(w, http.StatusForbidden, "permission_denied", "the API token does not grant downloader:manage")
		return
	}
	var request legacy.ImportRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	record, err := api.service.Import(r.Context(), request, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		switch {
		case errors.Is(err, legacy.ErrConfirmationRequired):
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
				"ok": false, "status": "blocked",
				"blockers":     []legacy.Issue{{Code: "confirm_import_required", Message: "必须显式设置 confirm_import=true。"}},
				"next_actions": []legacy.Issue{{Code: "preview_legacy_import", Message: "先读取迁移预览并核对 source_fingerprint。"}},
			})
		case errors.Is(err, legacy.ErrFingerprintMismatch):
			writeJSON(w, http.StatusConflict, map[string]any{
				"ok": false, "status": "blocked",
				"blockers":     []legacy.Issue{{Code: "source_fingerprint_mismatch", Message: "旧配置在预览后发生变化，拒绝执行。"}},
				"next_actions": []legacy.Issue{{Code: "preview_legacy_import", Message: "重新获取预览并人工核对新的 source_fingerprint。"}},
			})
		default:
			writeLegacySourceError(w, err)
		}
		return
	}
	status := http.StatusCreated
	if record.IdempotentReplay {
		status = http.StatusOK
	}
	if record.Status == "failed" || record.Status == "blocked" {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, legacyRecordEnvelope(record))
}

func (api legacyMigrationAPI) get(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "audit:read"); !ok {
		return
	}
	record, err := api.service.Get(r.Context(), r.PathValue("import_id"))
	if errors.Is(err, legacy.ErrImportNotFound) {
		writeProblem(w, http.StatusNotFound, "legacy_import_not_found", "legacy import was not found")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "legacy_import_read_failed", "legacy import could not be read")
		return
	}
	writeJSON(w, http.StatusOK, legacyRecordEnvelope(record))
}

func (api legacyMigrationAPI) list(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "audit:read"); !ok {
		return
	}
	limit := 25
	if raw := r.URL.Query().Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 100 {
			writeProblem(w, http.StatusBadRequest, "invalid_limit", "limit must be between 1 and 100")
			return
		}
		limit = parsed
	}
	imports, err := api.service.List(r.Context(), limit)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "legacy_import_list_failed", "legacy imports could not be listed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "imports": imports, "count": len(imports),
		"blockers": []any{}, "next_actions": []any{},
	})
}

func legacyRecordEnvelope(record legacy.ImportRecord) map[string]any {
	return map[string]any{
		"ok": record.OK, "status": record.Status, "import_id": record.ID,
		"source_fingerprint": record.SourceFingerprint,
		"idempotent_replay":  record.IdempotentReplay,
		"blockers":           record.Report.Blockers, "next_actions": record.Report.NextActions,
		"summary": record.Report.Summary, "import": record,
	}
}

func writeLegacySourceError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, legacy.ErrSourceUnavailable):
		writeJSON(w, http.StatusNotFound, map[string]any{
			"ok": false, "status": "blocked",
			"blockers":     []legacy.Issue{{Code: "legacy_source_unavailable", Message: "固定只读目录中未找到可读取的 config.py。"}},
			"next_actions": []legacy.Issue{{Code: "mount_legacy_data", Message: "将旧 data 目录只读挂载到 UA_LEGACY_DIR 后重新预览。"}},
		})
	case errors.Is(err, legacy.ErrSourceInvalid):
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
			"ok": false, "status": "blocked",
			"blockers":     []legacy.Issue{{Code: "legacy_source_invalid", Message: "旧配置不符合安全字面量解析或固定文件约束。"}},
			"next_actions": []legacy.Issue{{Code: "normalize_legacy_config", Message: "将配置整理为单一 config={...} 字面量；禁止 import、函数调用和符号链接。"}},
		})
	default:
		writeProblem(w, http.StatusInternalServerError, "legacy_migration_failed", "legacy migration could not be completed")
	}
}
