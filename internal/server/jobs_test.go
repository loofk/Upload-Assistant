package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type listJobService struct {
	JobService
	page   workflow.JobPage
	filter workflow.ListJobsFilter
}

type artifactJobService struct {
	JobService
	artifact workflow.Artifact
}

func (service artifactJobService) GetArtifact(context.Context, string, string) (workflow.Artifact, error) {
	return service.artifact, nil
}

type artifactReader struct {
	body  []byte
	calls int
}

func (reader *artifactReader) Read(context.Context, string, int64) ([]byte, error) {
	reader.calls++
	return append([]byte(nil), reader.body...), nil
}

func (service *listJobService) ListJobs(_ context.Context, filter workflow.ListJobsFilter) (workflow.JobPage, error) {
	service.filter = filter
	return service.page, nil
}

func TestListJobsUsesStableCursorAndFilters(t *testing.T) {
	createdAt := time.Date(2026, time.August, 8, 1, 2, 3, 4, time.UTC)
	job := workflow.Job{
		ID: "44444444-4444-4444-8444-444444444444", Kind: "retorrent", Status: workflow.JobBlocked,
		ExecutionMode: workflow.ExecutionStep, Input: json.RawMessage(`{"source_url":"https://u2.dmhy.org/download.php?id=1&passkey=list-secret"}`), Blockers: json.RawMessage(`[]`),
		NextActions: json.RawMessage(`[]`), ResumeState: json.RawMessage(`{}`), Summary: json.RawMessage(`{}`),
		CreatedAt: createdAt, UpdatedAt: createdAt,
	}
	service := &listJobService{page: workflow.JobPage{Jobs: []workflow.Job{job}, HasMore: true}}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/jobs?status=blocked&kind=retorrent&limit=1", nil)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "auditor", Role: "auditor", TokenScopes: []string{"jobs:read"},
	}))
	response := httptest.NewRecorder()
	(jobsAPI{service: service}).list(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("list status/body = %d/%s", response.Code, response.Body.String())
	}
	if service.filter.Status != workflow.JobBlocked || service.filter.Kind != "retorrent" || service.filter.Limit != 1 {
		t.Fatalf("list filter = %#v", service.filter)
	}
	var envelope struct {
		Jobs       []workflow.Job `json:"jobs"`
		HasMore    bool           `json:"has_more"`
		NextCursor string         `json:"next_cursor"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || len(envelope.Jobs) != 1 || !envelope.HasMore || envelope.NextCursor == "" {
		t.Fatalf("list envelope/error = %#v/%v", envelope, err)
	}
	if bytes.Contains(response.Body.Bytes(), []byte("list-secret")) {
		t.Fatalf("list response exposed source secret: %s", response.Body.String())
	}
	cursorTime, cursorID, err := decodeJobCursor(envelope.NextCursor)
	if err != nil || !cursorTime.Equal(createdAt) || cursorID != job.ID {
		t.Fatalf("cursor values/error = %s/%s/%v", cursorTime, cursorID, err)
	}
}

func TestListJobsRejectsMalformedCursor(t *testing.T) {
	service := &listJobService{}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/jobs?cursor=not-a-cursor", nil)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "auditor", Role: "auditor", TokenScopes: []string{"jobs:read"},
	}))
	response := httptest.NewRecorder()
	(jobsAPI{service: service}).list(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("malformed cursor status/body = %d/%s", response.Code, response.Body.String())
	}
}

func TestArtifactContentVerifiesEvidenceBeforeDownload(t *testing.T) {
	body := []byte(`{"ok":true,"status":"complete"}`)
	digest := sha256.Sum256(body)
	service := artifactJobService{artifact: workflow.Artifact{
		ID: "55555555-5555-4555-8555-555555555555", JobID: "44444444-4444-4444-8444-444444444444",
		Kind: "job_summary", StorageBackend: "local", StoragePath: "safe/summary.json", Filename: "summary.json",
		MIMEType: "application/json", SizeBytes: int64(len(body)), SHA256: fmt.Sprintf("%x", digest), CreatedAt: time.Now().UTC(),
	}}
	reader := &artifactReader{body: body}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/jobs/44444444-4444-4444-8444-444444444444/artifacts/55555555-5555-4555-8555-555555555555/content", nil)
	request.SetPathValue("job_id", service.artifact.JobID)
	request.SetPathValue("artifact_id", service.artifact.ID)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "auditor", Role: "auditor", TokenScopes: []string{"jobs:read", "audit:read"},
	}))
	response := httptest.NewRecorder()
	(jobsAPI{service: service, reader: reader}).artifactContent(response, request)
	if response.Code != http.StatusOK || response.Body.String() != string(body) || reader.calls != 1 {
		t.Fatalf("artifact content response/calls = %d/%s/%d", response.Code, response.Body.String(), reader.calls)
	}
	if response.Header().Get("Content-Disposition") == "" || response.Header().Get("Digest") == "" || response.Header().Get("Cache-Control") != "private, no-store" {
		t.Fatalf("artifact content headers = %#v", response.Header())
	}
}

func TestArtifactContentRejectsSecretBearingTorrent(t *testing.T) {
	service := artifactJobService{artifact: workflow.Artifact{
		ID: "55555555-5555-4555-8555-555555555555", JobID: "44444444-4444-4444-8444-444444444444",
		Kind: "source_torrent", StorageBackend: "local", StoragePath: "source.torrent", Filename: "source.torrent",
	}}
	reader := &artifactReader{body: []byte("announce-passkey")}
	request := httptest.NewRequest(http.MethodGet, "/content", nil)
	request.SetPathValue("job_id", service.artifact.JobID)
	request.SetPathValue("artifact_id", service.artifact.ID)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "auditor", Role: "auditor", TokenScopes: []string{"jobs:read", "audit:read"},
	}))
	response := httptest.NewRecorder()
	(jobsAPI{service: service, reader: reader}).artifactContent(response, request)
	if response.Code != http.StatusForbidden || reader.calls != 0 || bytes.Contains(response.Body.Bytes(), []byte("announce-passkey")) {
		t.Fatalf("restricted artifact response/calls = %d/%s/%d", response.Code, response.Body.String(), reader.calls)
	}
}
