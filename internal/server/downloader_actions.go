package server

import (
	"context"
	"encoding/base64"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxTorrentBase64Bytes = 44 << 20

type DownloaderService interface {
	Probe(context.Context, string, workflow.Actor) (qbittorrent.ProbeResult, error)
	Inspect(context.Context, string, string, workflow.Actor) (downloaders.TorrentEvidence, error)
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
	mux.HandleFunc("GET /api/v2/downloaders/{name}/torrents/{hash}", api.inspect)
	mux.HandleFunc("POST /api/v2/downloaders/{name}/torrents", api.add)
	mux.HandleFunc("POST /api/v2/downloaders/{name}/torrents/{hash}/limits", api.setLimits)
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
		writeDownloaderError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "limits_applied", "evidence": evidence,
		"blockers": []any{}, "next_actions": []any{},
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
