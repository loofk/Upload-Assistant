package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/candidates"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type CandidateService interface {
	Get(context.Context, string) (candidates.Item, error)
	List(context.Context, candidates.ListFilter) ([]candidates.Item, error)
	MarkSubmitted(context.Context, string, string) (candidates.Item, error)
}

type candidateAPI struct {
	store CandidateService
	jobs  JobService
}

type candidateListItem struct {
	candidates.Item
	RetorrentAction map[string]any `json:"retorrent_action,omitempty"`
}

type createCandidateJobRequest struct {
	Source        string                 `json:"source"`
	Target        string                 `json:"target"`
	TargetCount   int                    `json:"target_count,omitempty"`
	ScanLimit     int                    `json:"scan_limit,omitempty"`
	Page          int                    `json:"page,omitempty"`
	Date          string                 `json:"date,omitempty"`
	ExecutionMode workflow.ExecutionMode `json:"execution_mode,omitempty"`
	StopAfterStep string                 `json:"stop_after_step,omitempty"`
}

type submitCandidateRequest struct {
	ExecutionMode     workflow.ExecutionMode `json:"execution_mode,omitempty"`
	StopAfterStep     string                 `json:"stop_after_step,omitempty"`
	Downloader        json.RawMessage        `json:"downloader,omitempty"`
	TargetDownloader  json.RawMessage        `json:"target_downloader,omitempty"`
	Screenshots       json.RawMessage        `json:"screenshots,omitempty"`
	ImageHost         json.RawMessage        `json:"image_host,omitempty"`
	MetadataProviders json.RawMessage        `json:"metadata_providers,omitempty"`
	TargetPackage     json.RawMessage        `json:"target_package,omitempty"`
}

func registerCandidateRoutes(mux *http.ServeMux, store CandidateService, jobs JobService) {
	api := candidateAPI{store: store, jobs: jobs}
	mux.HandleFunc("GET /api/v2/candidates/daily", api.listDaily)
	mux.HandleFunc("POST /api/v2/candidates/daily", api.createDailyJob)
	mux.HandleFunc("POST /api/v2/candidates/{candidate_id}/retorrent-job", api.submitRetorrent)
}

func (api candidateAPI) listDaily(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 10, 1, 100)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	dateValue := strings.TrimSpace(r.URL.Query().Get("date"))
	if dateValue == "" {
		location := time.FixedZone("Asia/Shanghai", 8*60*60)
		dateValue = time.Now().In(location).Format("2006-01-02")
	}
	date, err := time.Parse("2006-01-02", dateValue)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_date", "date must use YYYY-MM-DD")
		return
	}
	status := candidates.Status(strings.TrimSpace(r.URL.Query().Get("status")))
	items, err := api.store.List(r.Context(), candidates.ListFilter{
		SourceSite: strings.TrimSpace(r.URL.Query().Get("source")), TargetSite: strings.TrimSpace(r.URL.Query().Get("target")),
		RecommendationDate: &date, Status: status, Limit: limit,
	})
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_candidate_filter", err.Error())
		return
	}
	readyCount := 0
	redacted := make([]candidateListItem, 0, len(items))
	now := time.Now()
	for _, item := range items {
		if item.Status == candidates.StatusCandidate && !item.ExpiresAt.After(now) {
			item.Status = candidates.StatusExpired
		}
		item.Payload = redactJSON(item.Payload)
		responseItem := candidateListItem{Item: item}
		if item.Status == candidates.StatusCandidate && item.Rank != nil {
			responseItem.RetorrentAction = map[string]any{
				"method": "POST", "path": "/api/v2/candidates/" + item.ID + "/retorrent-job",
				"requires": []string{"Idempotency-Key", "explicit rule acceptance before live execution", "explicit confirm_upload before live upload"},
			}
		}
		redacted = append(redacted, responseItem)
		if item.Status == candidates.StatusCandidate {
			readyCount++
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "date": dateValue, "count": len(redacted), "ready_count": readyCount,
		"candidates": redacted, "blockers": []any{}, "next_actions": []any{},
	})
}

func (api candidateAPI) createDailyJob(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" || len(idempotencyKey) > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_idempotency_key", "Idempotency-Key is required and must not exceed 200 characters")
		return
	}
	var request createCandidateJobRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	request.Source = strings.ToUpper(strings.TrimSpace(request.Source))
	request.Target = strings.ToUpper(strings.TrimSpace(request.Target))
	if request.Source == "" || request.Target == "" || request.Source == request.Target {
		writeProblem(w, http.StatusBadRequest, "invalid_candidate_sites", "different source and target site codes are required")
		return
	}
	if request.ExecutionMode != "" && request.ExecutionMode != workflow.ExecutionAuto && request.ExecutionMode != workflow.ExecutionStep {
		writeProblem(w, http.StatusBadRequest, "invalid_execution_mode", "execution_mode must be auto or step")
		return
	}
	if request.TargetCount < 0 || request.TargetCount > 25 || request.ScanLimit < 0 || request.ScanLimit > 100 ||
		(request.TargetCount > 0 && request.ScanLimit > 0 && request.ScanLimit < request.TargetCount) || request.Page < 0 || request.Page > 1000 {
		writeProblem(w, http.StatusBadRequest, "invalid_candidate_limits", "target_count must be at most 25, scan_limit at most 100 and not below target_count, and page at most 1000")
		return
	}
	if request.Date != "" {
		if _, err := time.Parse("2006-01-02", request.Date); err != nil {
			writeProblem(w, http.StatusBadRequest, "invalid_date", "date must use YYYY-MM-DD")
			return
		}
	}
	input, _ := json.Marshal(map[string]any{
		"source": request.Source, "target": request.Target, "target_count": request.TargetCount,
		"scan_limit": request.ScanLimit, "page": request.Page, "date": request.Date,
	})
	job, err := api.jobs.CreateJob(r.Context(), workflow.CreateJobInput{
		Kind: "daily_candidates", ExecutionMode: request.ExecutionMode, StopAfterStep: request.StopAfterStep,
		Input: input, IdempotencyKey: idempotencyKey, Owner: principal.UserID,
		Actor: workflow.Actor{Type: "user", ID: principal.UserID},
	})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, envelopeFor(job))
}

func (api candidateAPI) submitRetorrent(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	id := r.PathValue("candidate_id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_candidate_id", "candidate_id must be a UUID")
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" || len(idempotencyKey) > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_idempotency_key", "Idempotency-Key is required and must not exceed 200 characters")
		return
	}
	item, err := api.store.Get(r.Context(), id)
	if err != nil {
		writeCandidateError(w, err)
		return
	}
	if item.Status != candidates.StatusCandidate || item.Rank == nil || time.Now().After(item.ExpiresAt) {
		writeProblem(w, http.StatusConflict, "candidate_not_submittable", "candidate must be selected, ready, unexpired, and not previously submitted")
		return
	}
	var payload struct {
		Source struct {
			DetailsURL string `json:"details_url"`
		} `json:"source"`
		Ready bool `json:"ready"`
	}
	if err := json.Unmarshal(item.Payload, &payload); err != nil || !payload.Ready || strings.TrimSpace(payload.Source.DetailsURL) == "" {
		writeProblem(w, http.StatusConflict, "candidate_evidence_invalid", "candidate payload is missing immutable ready/source evidence")
		return
	}
	var request submitCandidateRequest
	if r.ContentLength != 0 {
		if err := decodeJSON(w, r, &request); err != nil {
			writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
			return
		}
	}
	if request.ExecutionMode == "" {
		request.ExecutionMode = workflow.ExecutionStep
	}
	if request.ExecutionMode != workflow.ExecutionAuto && request.ExecutionMode != workflow.ExecutionStep {
		writeProblem(w, http.StatusBadRequest, "invalid_execution_mode", "execution_mode must be auto or step")
		return
	}
	retorrentInput := map[string]any{
		"source_url": payload.Source.DetailsURL, "target": item.TargetSite,
		"candidate_id": item.ID, "candidate_discovery_job_id": item.DiscoveryJobID,
		"confirm_upload": false,
	}
	for key, value := range map[string]json.RawMessage{
		"downloader": request.Downloader, "target_downloader": request.TargetDownloader,
		"screenshots": request.Screenshots, "image_host": request.ImageHost, "metadata_providers": request.MetadataProviders, "target_package": request.TargetPackage,
	} {
		if len(value) > 0 {
			retorrentInput[key] = value
		}
	}
	body, _ := json.Marshal(retorrentInput)
	job, err := api.jobs.CreateJob(r.Context(), workflow.CreateJobInput{
		Kind: "retorrent", ExecutionMode: request.ExecutionMode, StopAfterStep: request.StopAfterStep,
		Input: body, IdempotencyKey: idempotencyKey, Owner: principal.UserID,
		Actor: workflow.Actor{Type: "user", ID: principal.UserID},
	})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	item, err = api.store.MarkSubmitted(r.Context(), item.ID, job.ID)
	if err != nil {
		// Job creation and candidate submission currently span two stores. Cancel
		// the newly created job when a concurrent request won the candidate so a
		// losing request cannot continue source/download work in the background.
		_, _ = api.jobs.CancelJob(r.Context(), job.ID, workflow.Actor{Type: "user", ID: principal.UserID})
		writeCandidateError(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]any{
		"ok": true, "status": job.Status, "job_id": job.ID, "candidate_id": item.ID,
		"blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions),
		"resume_state": redactJSON(job.ResumeState), "summary": redactJSON(job.Summary), "job": redactJob(job),
		"safety": map[string]any{"accept_rules_inferred": false, "confirm_upload": false, "live_upload_requires_explicit_confirmation": true},
	})
}

func writeCandidateError(w http.ResponseWriter, err error) {
	if errors.Is(err, candidates.ErrNotFound) {
		writeProblem(w, http.StatusNotFound, "candidate_not_found", "candidate was not found")
		return
	}
	if errors.Is(err, candidates.ErrNotSubmittable) {
		writeProblem(w, http.StatusConflict, "candidate_not_submittable", "candidate was already submitted, expired, or concurrently claimed; the newly created job was cancelled")
		return
	}
	writeProblem(w, http.StatusInternalServerError, "internal_error", "candidate request could not be completed")
}
