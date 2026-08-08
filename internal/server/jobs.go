package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type JobService interface {
	CreateJob(context.Context, workflow.CreateJobInput) (workflow.Job, error)
	GetJob(context.Context, string) (workflow.Job, error)
	ListJobs(context.Context, workflow.ListJobsFilter) (workflow.JobPage, error)
	ListSteps(context.Context, string) ([]workflow.Step, error)
	ListEvents(context.Context, string, int64, int) ([]workflow.Event, error)
	ListArtifacts(context.Context, string) ([]workflow.Artifact, error)
	GetArtifact(context.Context, string, string) (workflow.Artifact, error)
	PauseJob(context.Context, string, workflow.Actor) (workflow.Job, error)
	ResumeJob(context.Context, string, json.RawMessage, workflow.Actor) (workflow.Job, error)
	CancelJob(context.Context, string, workflow.Actor) (workflow.Job, error)
}

type ArtifactContentReader interface {
	Read(context.Context, string, int64) ([]byte, error)
}

type jobsAPI struct {
	service JobService
	reader  ArtifactContentReader
}

type createJobRequest struct {
	Kind          string                 `json:"kind"`
	ExecutionMode workflow.ExecutionMode `json:"execution_mode"`
	StopAfterStep string                 `json:"stop_after_step,omitempty"`
	Input         json.RawMessage        `json:"input"`
}

type resumeJobRequest struct {
	ResumeState json.RawMessage `json:"resume_state"`
}

type jobEnvelope struct {
	OK          bool               `json:"ok"`
	Status      workflow.JobStatus `json:"status"`
	JobID       string             `json:"job_id"`
	CurrentStep string             `json:"current_step,omitempty"`
	Blockers    json.RawMessage    `json:"blockers"`
	NextActions json.RawMessage    `json:"next_actions"`
	ResumeState json.RawMessage    `json:"resume_state"`
	Summary     json.RawMessage    `json:"summary"`
	Job         workflow.Job       `json:"job"`
}

func registerJobRoutes(mux *http.ServeMux, service JobService, reader ArtifactContentReader) {
	api := jobsAPI{service: service, reader: reader}
	mux.HandleFunc("POST /api/v2/jobs", api.create)
	mux.HandleFunc("GET /api/v2/jobs", api.list)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}", api.get)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/summary", api.summary)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/steps", api.steps)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/events", api.events)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/artifacts", api.artifacts)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/artifacts/{artifact_id}/content", api.artifactContent)
	mux.HandleFunc("POST /api/v2/jobs/{job_id}/pause", api.pause)
	mux.HandleFunc("POST /api/v2/jobs/{job_id}/resume", api.resume)
	mux.HandleFunc("POST /api/v2/jobs/{job_id}/cancel", api.cancel)
}

const maxArtifactContentBytes = 64 << 20

var downloadableArtifactKinds = map[string]struct{}{
	"content_manifest": {}, "metadata": {}, "mediainfo": {}, "bdinfo": {}, "screenshot": {},
	"image_upload_receipt": {}, "target_package": {}, "duplicate_check": {},
	"target_torrent_receipt": {}, "preupload_duplicate_check": {}, "target_upload_receipt": {},
	"target_torrent_download_receipt": {}, "target_injection_receipt": {},
	"target_seed_observation": {}, "job_summary": {},
	"candidate_scan": {}, "candidate_evaluation": {}, "candidate_digest": {}, "candidate_summary": {},
}

func (a jobsAPI) artifactContent(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:read")
	if !ok {
		return
	}
	if !principal.HasScope("audit:read") {
		writeProblem(w, http.StatusForbidden, "permission_denied", "the API token does not grant audit:read")
		return
	}
	jobID, ok := jobID(w, r)
	if !ok {
		return
	}
	artifactID := r.PathValue("artifact_id")
	if _, err := uuid.Parse(artifactID); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_artifact_id", "artifact_id must be a UUID")
		return
	}
	artifact, err := a.service.GetArtifact(r.Context(), jobID, artifactID)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	if _, allowed := downloadableArtifactKinds[artifact.Kind]; !allowed {
		writeProblem(w, http.StatusForbidden, "artifact_content_restricted", "raw torrent and secret-bearing artifact content cannot be downloaded through the API")
		return
	}
	if !artifact.ExpiresAt.IsZero() && time.Now().UTC().After(artifact.ExpiresAt) {
		writeProblem(w, http.StatusGone, "artifact_content_expired", "artifact retention has expired")
		return
	}
	if artifact.StorageBackend != "local" || artifact.SizeBytes < 0 || artifact.SizeBytes > maxArtifactContentBytes || a.reader == nil {
		writeProblem(w, http.StatusServiceUnavailable, "artifact_content_unavailable", "artifact content is unavailable through this service instance")
		return
	}
	body, err := a.reader.Read(r.Context(), artifact.StoragePath, maxArtifactContentBytes)
	if err != nil {
		writeProblem(w, http.StatusGone, "artifact_content_unavailable", "artifact content is missing, expired, or unreadable")
		return
	}
	digest := sha256.Sum256(body)
	computedSHA := hex.EncodeToString(digest[:])
	if int64(len(body)) != artifact.SizeBytes || !strings.EqualFold(computedSHA, artifact.SHA256) {
		writeProblem(w, http.StatusConflict, "artifact_integrity_failed", "artifact content no longer matches its immutable size and SHA-256 evidence")
		return
	}
	contentType := artifact.MIMEType
	if !(strings.HasPrefix(contentType, "image/") || contentType == "application/json" || strings.HasPrefix(contentType, "text/plain")) {
		contentType = "application/octet-stream"
	}
	disposition := mime.FormatMediaType("attachment", map[string]string{"filename": artifact.Filename})
	if disposition == "" {
		disposition = "attachment"
	}
	w.Header().Set("Cache-Control", "private, no-store")
	w.Header().Set("Content-Disposition", disposition)
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Digest", "sha-256=:"+base64.StdEncoding.EncodeToString(digest[:])+":")
	w.Header().Set("ETag", `"`+computedSHA+`"`)
	http.ServeContent(w, r, artifact.Filename, artifact.CreatedAt, bytes.NewReader(body))
}

func (a jobsAPI) list(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	limit, err := parseIntQuery(r, "limit", 25, 1, 100)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	status := workflow.JobStatus(strings.TrimSpace(r.URL.Query().Get("status")))
	if status != "" && !validJobStatus(status) {
		writeProblem(w, http.StatusBadRequest, "invalid_status", "status is not a supported job state")
		return
	}
	kind := strings.TrimSpace(r.URL.Query().Get("kind"))
	if kind != "" && kind != "retorrent" && kind != "daily_candidates" {
		writeProblem(w, http.StatusBadRequest, "invalid_kind", "kind must be retorrent or daily_candidates")
		return
	}
	filter := workflow.ListJobsFilter{Status: status, Kind: kind, Limit: limit}
	if cursor := strings.TrimSpace(r.URL.Query().Get("cursor")); cursor != "" {
		createdAt, id, err := decodeJobCursor(cursor)
		if err != nil {
			writeProblem(w, http.StatusBadRequest, "invalid_cursor", "cursor is invalid or malformed")
			return
		}
		filter.BeforeCreatedAt, filter.BeforeID = &createdAt, id
	}
	page, err := a.service.ListJobs(r.Context(), filter)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	nextCursor := ""
	if page.HasMore && len(page.Jobs) > 0 {
		nextCursor = encodeJobCursor(page.Jobs[len(page.Jobs)-1])
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "jobs": redactJobs(page.Jobs),
		"has_more": page.HasMore, "next_cursor": nextCursor,
		"blockers": []any{}, "next_actions": []any{},
	})
}

func (a jobsAPI) summary(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.GetJob(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	steps, err := a.service.ListSteps(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	artifacts, err := a.service.ListArtifacts(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": jobOK(job.Status), "status": job.Status, "job_id": id, "kind": job.Kind,
		"current_step": job.CurrentStep, "blockers": redactJSON(job.Blockers),
		"next_actions": redactJSON(job.NextActions), "resume_state": redactJSON(job.ResumeState),
		"summary": redactJSON(job.Summary), "steps": redactSteps(steps), "artifacts": redactArtifacts(artifacts),
	})
}

func (a jobsAPI) create(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" || len(idempotencyKey) > 200 {
		writeProblem(w, http.StatusBadRequest, "invalid_idempotency_key", "Idempotency-Key is required and must not exceed 200 characters")
		return
	}
	var request createJobRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if request.Kind == "" {
		request.Kind = "retorrent"
	}
	if request.Kind != "retorrent" && request.Kind != "daily_candidates" {
		writeProblem(w, http.StatusBadRequest, "unsupported_job_kind", "kind must be retorrent or daily_candidates")
		return
	}
	job, err := a.service.CreateJob(r.Context(), workflow.CreateJobInput{
		Kind:           request.Kind,
		ExecutionMode:  request.ExecutionMode,
		StopAfterStep:  request.StopAfterStep,
		Input:          request.Input,
		IdempotencyKey: idempotencyKey,
		Owner:          principal.UserID,
		Actor:          workflow.Actor{Type: "user", ID: principal.UserID},
	})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, envelopeFor(job))
}

func (a jobsAPI) get(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.GetJob(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envelopeFor(job))
}

func (a jobsAPI) steps(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.GetJob(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	steps, err := a.service.ListSteps(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": job.Status, "job_id": id, "current_step": job.CurrentStep,
		"blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions), "steps": redactSteps(steps),
	})
}

func (a jobsAPI) events(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	after, err := parseIntQuery(r, "after", 0, 0, 1<<62)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_cursor", err.Error())
		return
	}
	limit, err := parseIntQuery(r, "limit", 100, 1, 500)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_limit", err.Error())
		return
	}
	if _, err := a.service.GetJob(r.Context(), id); err != nil {
		writeWorkflowError(w, err)
		return
	}
	events, err := a.service.ListEvents(r.Context(), id, int64(after), limit)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	nextCursor := int64(after)
	if len(events) > 0 {
		nextCursor = events[len(events)-1].Sequence
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "job_id": id, "events": redactEvents(events), "next_cursor": nextCursor,
	})
}

func (a jobsAPI) artifacts(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.GetJob(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	artifacts, err := a.service.ListArtifacts(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": job.Status, "job_id": id, "artifacts": redactArtifacts(artifacts),
		"blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions),
	})
}

func (a jobsAPI) pause(w http.ResponseWriter, r *http.Request) {
	principal, allowed := requireScope(w, r, "jobs:write")
	if !allowed {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.PauseJob(r.Context(), id, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envelopeFor(job))
}

func (a jobsAPI) resume(w http.ResponseWriter, r *http.Request) {
	principal, allowed := requireScope(w, r, "jobs:write")
	if !allowed {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	request := resumeJobRequest{ResumeState: json.RawMessage(`{}`)}
	if r.ContentLength != 0 {
		if err := decodeJSON(w, r, &request); err != nil {
			writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
			return
		}
	}
	job, err := a.service.ResumeJob(r.Context(), id, request.ResumeState, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envelopeFor(job))
}

func (a jobsAPI) cancel(w http.ResponseWriter, r *http.Request) {
	principal, allowed := requireScope(w, r, "jobs:write")
	if !allowed {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.CancelJob(r.Context(), id, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envelopeFor(job))
}

func envelopeFor(job workflow.Job) jobEnvelope {
	job = redactJob(job)
	return jobEnvelope{
		OK: jobOK(job.Status), Status: job.Status, JobID: job.ID,
		CurrentStep: job.CurrentStep, Blockers: job.Blockers, NextActions: job.NextActions,
		ResumeState: job.ResumeState, Summary: job.Summary, Job: job,
	}
}

func jobOK(status workflow.JobStatus) bool {
	return status != workflow.JobBlocked && status != workflow.JobFailed && status != workflow.JobCancelled
}

type jobCursor struct {
	CreatedAt time.Time `json:"created_at"`
	ID        string    `json:"id"`
}

func encodeJobCursor(job workflow.Job) string {
	body, _ := json.Marshal(jobCursor{CreatedAt: job.CreatedAt.UTC(), ID: job.ID})
	return base64.RawURLEncoding.EncodeToString(body)
}

func decodeJobCursor(value string) (time.Time, string, error) {
	body, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || len(body) > 512 {
		return time.Time{}, "", errors.New("invalid job cursor encoding")
	}
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.DisallowUnknownFields()
	var cursor jobCursor
	if err := decoder.Decode(&cursor); err != nil || cursor.CreatedAt.IsZero() {
		return time.Time{}, "", errors.New("invalid job cursor value")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return time.Time{}, "", errors.New("invalid job cursor document")
	}
	if _, err := uuid.Parse(cursor.ID); err != nil {
		return time.Time{}, "", errors.New("invalid job cursor id")
	}
	return cursor.CreatedAt.UTC(), cursor.ID, nil
}

func validJobStatus(status workflow.JobStatus) bool {
	switch status {
	case workflow.JobDraft, workflow.JobQueued, workflow.JobRunning, workflow.JobPaused,
		workflow.JobBlocked, workflow.JobFailed, workflow.JobComplete, workflow.JobCancelled:
		return true
	default:
		return false
	}
}

func jobID(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("job_id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_job_id", "job_id must be a UUID")
		return "", false
	}
	return id, true
}

func decodeJSON(w http.ResponseWriter, r *http.Request, destination any) error {
	return decodeJSONLimit(w, r, destination, 1<<20)
}

func decodeJSONLimit(w http.ResponseWriter, r *http.Request, destination any, limit int64) error {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, limit))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return fmt.Errorf("decode JSON body: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("JSON body must contain a single object")
	}
	return nil
}

func parseIntQuery(r *http.Request, name string, fallback, minimum, maximum int) (int, error) {
	value := strings.TrimSpace(r.URL.Query().Get(name))
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < minimum || parsed > maximum {
		return 0, fmt.Errorf("%s must be between %d and %d", name, minimum, maximum)
	}
	return parsed, nil
}

func writeWorkflowError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, workflow.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "not_found", "job was not found")
	case errors.Is(err, workflow.ErrConflict):
		writeProblem(w, http.StatusConflict, "state_conflict", err.Error())
	case errors.Is(err, workflow.ErrUnsupportedKind):
		writeProblem(w, http.StatusBadRequest, "unsupported_job_kind", err.Error())
	default:
		writeProblem(w, http.StatusInternalServerError, "internal_error", "request could not be completed")
	}
}

func writeProblem(w http.ResponseWriter, status int, code, detail string) {
	writeJSON(w, status, map[string]any{
		"ok": false, "status": "failed", "error": map[string]string{"code": code, "detail": detail},
		"blockers": []map[string]string{{"code": code, "message": detail}}, "next_actions": []any{},
	})
}

func requireScope(w http.ResponseWriter, r *http.Request, scope string) (security.Principal, bool) {
	principal, ok := security.PrincipalFromContext(r.Context())
	if !ok {
		writeProblem(w, http.StatusUnauthorized, "authentication_required", "an authenticated principal is required")
		return security.Principal{}, false
	}
	if !principal.HasScope(scope) {
		writeProblem(w, http.StatusForbidden, "permission_denied", "the API token does not grant "+scope)
		return security.Principal{}, false
	}
	return principal, true
}
