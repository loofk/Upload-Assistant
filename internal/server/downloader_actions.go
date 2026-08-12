package server

import (
	"context"
	"encoding/base64"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxTorrentBase64Bytes = 44 << 20

type DownloaderService interface {
	Probe(context.Context, string, workflow.Actor) (qbittorrent.ProbeResult, error)
	Dashboard(context.Context, string, downloaders.DashboardQuery) (downloaders.DashboardSnapshot, error)
	Inspect(context.Context, string, string, workflow.Actor) (downloaders.TorrentEvidence, error)
	Files(context.Context, string, string, workflow.Actor) (downloaders.TorrentFilesEvidence, error)
	Add(context.Context, string, []byte, qbittorrent.AddOptions, workflow.Actor) (downloaders.AddEvidence, error)
	SetLimits(context.Context, string, string, int64, int64, workflow.Actor) (downloaders.TorrentEvidence, error)
}

type downloaderActionsAPI struct {
	service DownloaderService
}

type addTorrentRequest struct {
	TorrentBase64 string   `json:"torrent_base64"`
	SavePath      string   `json:"save_path,omitempty"`
	Category      string   `json:"category,omitempty"`
	Tags          []string `json:"tags,omitempty"`
	ApplyLabels   *bool    `json:"apply_labels,omitempty"`
	SkipChecking  bool     `json:"skip_checking,omitempty"`
	Paused        bool     `json:"paused,omitempty"`
	UploadLimit   int64    `json:"upload_limit,omitempty"`
	DownloadLimit int64    `json:"download_limit,omitempty"`
}

type setLimitsRequest struct {
	DownloadLimit int64 `json:"download_limit"`
	UploadLimit   int64 `json:"upload_limit"`
}

func registerDownloaderRoutes(mux *http.ServeMux, service DownloaderService) {
	api := downloaderActionsAPI{service: service}
	mux.HandleFunc("POST /api/v2/downloaders/{name}/probe", api.probe)
	mux.HandleFunc("GET /api/v2/downloaders/{name}/snapshot", api.dashboard)
	mux.HandleFunc("GET /api/v2/downloaders/{name}/torrents/{hash}", api.inspect)
	mux.HandleFunc("GET /api/v2/downloaders/{name}/torrents/{hash}/files", api.files)
	mux.HandleFunc("POST /api/v2/downloaders/{name}/torrents", api.add)
	mux.HandleFunc("POST /api/v2/downloaders/{name}/torrents/{hash}/limits", api.setLimits)
}

func (api downloaderActionsAPI) dashboard(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "downloader:manage"); !ok {
		return
	}
	offset, err := optionalNonNegativeInt(r.URL.Query().Get("offset"), 0)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", "offset must be a non-negative integer")
		return
	}
	limit, err := optionalNonNegativeInt(r.URL.Query().Get("limit"), 100)
	if err != nil || limit < 1 || limit > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_request", "limit must be between 1 and 200")
		return
	}
	filter := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("filter")))
	if filter == "" {
		filter = "all"
	}
	if !validDashboardFilter(filter) {
		writeProblem(w, http.StatusBadRequest, "invalid_request", "filter is not supported")
		return
	}
	search := strings.TrimSpace(r.URL.Query().Get("query"))
	if utf8.RuneCountInString(search) > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_request", "query must not exceed 200 characters")
		return
	}
	snapshot, err := api.service.Dashboard(r.Context(), strings.TrimSpace(r.PathValue("name")), downloaders.DashboardQuery{
		Filter: filter, Query: search, Offset: offset, Limit: limit,
	})
	if err != nil {
		writeDownloaderError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "snapshot": snapshot, "blockers": []any{}, "next_actions": []any{},
	})
}

func validDashboardFilter(value string) bool {
	switch value {
	case "all", "downloading", "seeding", "active", "paused", "checking", "error", "completed":
		return true
	default:
		return false
	}
}

func optionalNonNegativeInt(value string, fallback int) (int, error) {
	if strings.TrimSpace(value) == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return 0, errors.New("invalid non-negative integer")
	}
	return parsed, nil
}

func (api downloaderActionsAPI) probe(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "downloader:manage")
	if !ok {
		return
	}
	result, err := api.service.Probe(
		r.Context(), strings.TrimSpace(r.PathValue("name")),
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeDownloaderError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "downloader": r.PathValue("name"),
		"probe": result, "blockers": []any{}, "next_actions": []any{},
	})
}

func (api downloaderActionsAPI) inspect(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "downloader:manage")
	if !ok {
		return
	}
	evidence, err := api.service.Inspect(
		r.Context(), strings.TrimSpace(r.PathValue("name")), strings.TrimSpace(r.PathValue("hash")),
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeDownloaderError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "evidence": evidence,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api downloaderActionsAPI) files(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "downloader:manage")
	if !ok {
		return
	}
	offset, parseErr := optionalNonNegativeInt(r.URL.Query().Get("offset"), 0)
	if parseErr != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", "offset must be a non-negative integer")
		return
	}
	limit, parseErr := optionalNonNegativeInt(r.URL.Query().Get("limit"), 100)
	if parseErr != nil || limit < 1 || limit > 500 {
		writeProblem(w, http.StatusBadRequest, "invalid_request", "limit must be between 1 and 500")
		return
	}
	evidence, err := api.service.Files(
		r.Context(), strings.TrimSpace(r.PathValue("name")), strings.TrimSpace(r.PathValue("hash")),
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		writeDownloaderError(w, err)
		return
	}
	start := offset
	if start > len(evidence.Files) {
		start = len(evidence.Files)
	}
	end := start + limit
	if end > len(evidence.Files) {
		end = len(evidence.Files)
	}
	evidence.Files = evidence.Files[start:end]
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "evidence": evidence,
		"offset": start, "limit": limit, "has_more": end < evidence.FileCount,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (api downloaderActionsAPI) add(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "downloader:manage")
	if !ok {
		return
	}
	var request addTorrentRequest
	if err := decodeJSONLimit(w, r, &request, maxTorrentBase64Bytes+(1<<20)); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if request.TorrentBase64 == "" || len(request.TorrentBase64) > maxTorrentBase64Bytes {
		writeProblem(w, http.StatusBadRequest, "invalid_torrent", "torrent_base64 is required and must not exceed the request limit")
		return
	}
	metainfo, err := base64.StdEncoding.DecodeString(request.TorrentBase64)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_torrent", "torrent_base64 is not valid base64")
		return
	}
	evidence, err := api.service.Add(
		r.Context(), strings.TrimSpace(r.PathValue("name")), metainfo,
		qbittorrent.AddOptions{
			SavePath: request.SavePath, Category: request.Category, Tags: request.Tags,
			ApplyLabels:  request.ApplyLabels,
			SkipChecking: request.SkipChecking, Paused: request.Paused,
			UploadLimit: request.UploadLimit, DownloadLimit: request.DownloadLimit,
		}, workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		if errors.Is(err, downloaders.ErrAddOutcomeUnknown) {
			writeDownloaderAddReconciliation(w, "downloader_add_outcome_unknown", "the downloader add result is not trustworthy; inspect the expected hash before any further write", evidence)
			return
		}
		if _, partial := downloaders.PartialAddHash(err); partial {
			writeDownloaderAddReconciliation(w, "downloader_partial_add_requires_reconciliation", "the downloader applied only part of the requested add operation; inspect the expected hash and settings before any further write", evidence)
			return
		}
		writeDownloaderError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "added", "evidence": evidence,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func writeDownloaderAddReconciliation(w http.ResponseWriter, code, message string, evidence downloaders.AddEvidence) {
	expectedHash := evidence.ExpectedHashes.V1SHA1
	if expectedHash == "" {
		expectedHash = evidence.ExpectedHashes.V2SHA256
	}
	if expectedHash == "" {
		expectedHash = evidence.Result.Hashes.V1SHA1
	}
	if expectedHash == "" {
		expectedHash = evidence.Result.Hashes.V2SHA256
	}
	writeJSON(w, http.StatusConflict, map[string]any{
		"ok": false, "status": "blocked", "evidence": evidence,
		"error":    map[string]string{"code": code, "detail": message},
		"blockers": []map[string]string{{"code": code, "message": message}},
		"next_actions": []map[string]any{{
			"action":      "inspect_torrent_before_retry",
			"description": "Use the read-only torrent inspection endpoint for the exact expected hash. Do not repeat the add request blindly.",
			"parameters":  map[string]any{"downloader_name": evidence.DownloaderName, "observed_hash": expectedHash},
		}},
	})
}

func (api downloaderActionsAPI) setLimits(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "downloader:manage")
	if !ok {
		return
	}
	var request setLimitsRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	evidence, err := api.service.SetLimits(
		r.Context(), strings.TrimSpace(r.PathValue("name")), strings.TrimSpace(r.PathValue("hash")),
		request.DownloadLimit, request.UploadLimit,
		workflow.Actor{Type: "user", ID: principal.UserID},
	)
	if err != nil {
		if errors.Is(err, downloaders.ErrLimitsOutcomeUnknown) {
			writeDownloaderLimitReconciliation(
				w, strings.TrimSpace(r.PathValue("name")), strings.ToLower(strings.TrimSpace(r.PathValue("hash"))),
				request.DownloadLimit, request.UploadLimit, evidence,
			)
			return
		}
		writeDownloaderError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "limits_applied", "evidence": evidence,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func writeDownloaderLimitReconciliation(w http.ResponseWriter, name, hash string, downloadLimit, uploadLimit int64, evidence downloaders.TorrentEvidence) {
	message := "the downloader limit result is not trustworthy; inspect the exact torrent before deciding whether to apply the limits again"
	writeJSON(w, http.StatusConflict, map[string]any{
		"ok": false, "status": "blocked", "evidence": evidence,
		"expected": map[string]any{
			"downloader_name": name, "torrent_hash": hash,
			"download_limit": downloadLimit, "upload_limit": uploadLimit,
		},
		"error":    map[string]string{"code": "downloader_limits_outcome_unknown", "detail": message},
		"blockers": []map[string]string{{"code": "downloader_limits_outcome_unknown", "message": message}},
		"next_actions": []map[string]any{{
			"action":      "inspect_torrent_limits_before_retry",
			"description": "Use the read-only torrent inspection endpoint and compare both effective limits with the expected caps. Do not repeat this write blindly.",
			"parameters":  map[string]any{"downloader_name": name, "torrent_hash": hash},
		}},
	})
}

func writeDownloaderError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, integrations.ErrNotFound), errors.Is(err, qbittorrent.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "downloader_resource_not_found", "the downloader or torrent was not found")
	case errors.Is(err, integrations.ErrValidation):
		writeProblem(w, http.StatusConflict, "downloader_not_ready", err.Error())
	case errors.Is(err, downloaders.ErrAdapterUnavailable):
		writeProblem(w, http.StatusConflict, "downloader_adapter_unavailable", err.Error())
	case isPartialDownloaderAdd(err):
		writeProblem(w, http.StatusConflict, "downloader_partial_add_requires_reconciliation", err.Error())
	case errors.Is(err, downloaders.ErrAddOutcomeUnknown):
		writeProblem(w, http.StatusConflict, "downloader_add_outcome_unknown", "the downloader add result is not trustworthy and must be reconciled")
	case errors.Is(err, downloaders.ErrLimitsOutcomeUnknown):
		writeProblem(w, http.StatusConflict, "downloader_limits_outcome_unknown", "the downloader limit result is not trustworthy and must be reconciled")
	case errors.Is(err, qbittorrent.ErrUnauthorized):
		writeProblem(w, http.StatusBadGateway, "downloader_authentication_failed", "the downloader rejected the configured credentials")
	case errors.Is(err, context.DeadlineExceeded):
		writeProblem(w, http.StatusGatewayTimeout, "downloader_timeout", "the downloader request timed out")
	default:
		writeProblem(w, http.StatusBadGateway, "downloader_request_failed", "the downloader request could not be completed")
	}
}

func isPartialDownloaderAdd(err error) bool {
	_, ok := downloaders.PartialAddHash(err)
	return ok
}
