package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type JobService interface {
	CreateJob(context.Context, workflow.CreateJobInput) (workflow.Job, error)
	GetJob(context.Context, string) (workflow.Job, error)
	ListSteps(context.Context, string) ([]workflow.Step, error)
	ListEvents(context.Context, string, int64, int) ([]workflow.Event, error)
	ListArtifacts(context.Context, string) ([]workflow.Artifact, error)
	PauseJob(context.Context, string, workflow.Actor) (workflow.Job, error)
	ResumeJob(context.Context, string, json.RawMessage, workflow.Actor) (workflow.Job, error)
	CancelJob(context.Context, string, workflow.Actor) (workflow.Job, error)
}

type jobsAPI struct {
	service JobService
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

func registerJobRoutes(mux *http.ServeMux, service JobService) {
	api := jobsAPI{service: service}
	mux.HandleFunc("POST /api/v2/jobs", api.create)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}", api.get)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/steps", api.steps)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/events", api.events)
	mux.HandleFunc("GET /api/v2/jobs/{job_id}/artifacts", api.artifacts)
	mux.HandleFunc("POST /api/v2/jobs/{job_id}/pause", api.pause)
	mux.HandleFunc("POST /api/v2/jobs/{job_id}/resume", api.resume)
	mux.HandleFunc("POST /api/v2/jobs/{job_id}/cancel", api.cancel)
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
	if request.Kind != "retorrent" {
		writeProblem(w, http.StatusBadRequest, "unsupported_job_kind", "only retorrent jobs are available in this build")
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
		"blockers": job.Blockers, "next_actions": job.NextActions, "steps": steps,
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
		"ok": true, "status": "ready", "job_id": id, "events": events, "next_cursor": nextCursor,
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
		"ok": true, "status": job.Status, "job_id": id, "artifacts": artifacts,
		"blockers": job.Blockers, "next_actions": job.NextActions,
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
	return jobEnvelope{
		OK: job.Status != workflow.JobFailed, Status: job.Status, JobID: job.ID,
		CurrentStep: job.CurrentStep, Blockers: job.Blockers, NextActions: job.NextActions,
		ResumeState: job.ResumeState, Summary: job.Summary, Job: job,
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
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
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
