package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/candidates"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeCandidateStore struct {
	item       candidates.Item
	items      []candidates.Item
	listFilter candidates.ListFilter
	markedJob  string
	markErr    error
}

func (store *fakeCandidateStore) Get(context.Context, string) (candidates.Item, error) {
	return store.item, nil
}

func (store *fakeCandidateStore) List(_ context.Context, filter candidates.ListFilter) ([]candidates.Item, error) {
	store.listFilter = filter
	return store.items, nil
}

func (store *fakeCandidateStore) MarkSubmitted(_ context.Context, _ string, jobID string) (candidates.Item, error) {
	if store.markErr != nil {
		return candidates.Item{}, store.markErr
	}
	store.markedJob = jobID
	store.item.Status = candidates.StatusSubmitted
	store.item.SubmittedJobID = jobID
	return store.item, nil
}

type createCaptureJobService struct {
	JobService
	input        workflow.CreateJobInput
	job          workflow.Job
	cancelledJob string
}

func (service *createCaptureJobService) CreateJob(_ context.Context, input workflow.CreateJobInput) (workflow.Job, error) {
	service.input = input
	return service.job, nil
}

func (service *createCaptureJobService) CancelJob(_ context.Context, id string, _ workflow.Actor) (workflow.Job, error) {
	service.cancelledJob = id
	service.job.Status = workflow.JobCancelled
	return service.job, nil
}

func candidateRequest(method, target, body string, scopes ...string) *http.Request {
	request := httptest.NewRequest(method, target, bytes.NewBufferString(body))
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "operator", Role: "admin", TokenScopes: scopes,
	}))
	return request
}

func TestCreateDailyCandidateJobUsesDurableWorkflow(t *testing.T) {
	jobs := &createCaptureJobService{job: workflow.Job{
		ID: "44444444-4444-4444-8444-444444444444", Kind: "daily_candidates",
		Status: workflow.JobQueued, Blockers: json.RawMessage(`[]`), NextActions: json.RawMessage(`[]`),
		ResumeState: json.RawMessage(`{}`), Summary: json.RawMessage(`{}`), Input: json.RawMessage(`{}`),
	}}
	request := candidateRequest(http.MethodPost, "/api/v2/candidates/daily", `{"source":"U2","target":"MTEAM","target_count":10,"scan_limit":30}`, "jobs:write")
	request.Header.Set("Idempotency-Key", "daily-2026-08-08-U2-MTEAM")
	response := httptest.NewRecorder()
	(candidateAPI{store: &fakeCandidateStore{}, jobs: jobs}).createDailyJob(response, request)
	if response.Code != http.StatusAccepted || jobs.input.Kind != "daily_candidates" || jobs.input.IdempotencyKey == "" {
		t.Fatalf("response/input = %d/%s/%#v", response.Code, response.Body.String(), jobs.input)
	}
	var input dailyCandidateInputForTest
	if err := json.Unmarshal(jobs.input.Input, &input); err != nil || input.Source != "U2" || input.TargetCount != 10 {
		t.Fatalf("job input/error = %#v/%v", input, err)
	}
}

type dailyCandidateInputForTest struct {
	Source      string `json:"source"`
	TargetCount int    `json:"target_count"`
}

func TestSubmitCandidateCreatesUnconfirmedRetorrentJob(t *testing.T) {
	rank := 1
	store := &fakeCandidateStore{item: candidates.Item{
		ID: "55555555-5555-4555-8555-555555555555", DiscoveryJobID: "44444444-4444-4444-8444-444444444444",
		SourceSite: "U2", TargetSite: "MTEAM", SourceTorrentID: "60635", Rank: &rank,
		Status: candidates.StatusCandidate, ExpiresAt: time.Now().Add(time.Hour),
		Payload: json.RawMessage(`{"ready":true,"source":{"details_url":"https://u2.dmhy.org/details.php?id=60635"}}`),
	}}
	jobs := &createCaptureJobService{job: workflow.Job{
		ID: "66666666-6666-4666-8666-666666666666", Kind: "retorrent", Status: workflow.JobQueued,
		Blockers: json.RawMessage(`[]`), NextActions: json.RawMessage(`[]`), ResumeState: json.RawMessage(`{}`), Summary: json.RawMessage(`{}`), Input: json.RawMessage(`{}`),
	}}
	request := candidateRequest(http.MethodPost, "/api/v2/candidates/55555555-5555-4555-8555-555555555555/retorrent-job", `{"execution_mode":"step","downloader":{"name":"box","save_path":"/downloads"}}`, "jobs:write")
	request.SetPathValue("candidate_id", store.item.ID)
	request.Header.Set("Idempotency-Key", "submit-candidate-60635")
	response := httptest.NewRecorder()
	(candidateAPI{store: store, jobs: jobs}).submitRetorrent(response, request)
	if response.Code != http.StatusAccepted || jobs.input.Kind != "retorrent" || store.markedJob != jobs.job.ID {
		t.Fatalf("response/input/marked = %d/%s/%#v/%s", response.Code, response.Body.String(), jobs.input, store.markedJob)
	}
	var input map[string]any
	if err := json.Unmarshal(jobs.input.Input, &input); err != nil {
		t.Fatal(err)
	}
	if input["source_url"] != "https://u2.dmhy.org/details.php?id=60635" || input["target"] != "MTEAM" || input["confirm_upload"] != false {
		t.Fatalf("retorrent input = %#v", input)
	}
	if _, exists := input["accept_rules"]; exists {
		t.Fatalf("candidate submission inferred rule acceptance: %#v", input)
	}
	if bytes.Contains(response.Body.Bytes(), []byte(`"confirm_upload":true`)) {
		t.Fatalf("candidate response inferred upload confirmation: %s", response.Body.String())
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || len(envelope["summary"]) == 0 {
		t.Fatalf("candidate response does not satisfy job envelope: %s / %v", response.Body.String(), err)
	}
}

func TestCandidateAPIsRejectInvalidExecutionMode(t *testing.T) {
	jobs := &createCaptureJobService{}
	createRequest := candidateRequest(http.MethodPost, "/api/v2/candidates/daily", `{"source":"U2","target":"MTEAM","execution_mode":"unsafe"}`, "jobs:write")
	createRequest.Header.Set("Idempotency-Key", "invalid-mode-create")
	createResponse := httptest.NewRecorder()
	(candidateAPI{store: &fakeCandidateStore{}, jobs: jobs}).createDailyJob(createResponse, createRequest)
	if createResponse.Code != http.StatusBadRequest {
		t.Fatalf("create response = %d/%s", createResponse.Code, createResponse.Body.String())
	}

	rank := 1
	store := &fakeCandidateStore{item: candidates.Item{
		ID: "55555555-5555-4555-8555-555555555555", Rank: &rank, Status: candidates.StatusCandidate,
		ExpiresAt: time.Now().Add(time.Hour), TargetSite: "MTEAM",
		Payload: json.RawMessage(`{"ready":true,"source":{"details_url":"https://u2.dmhy.org/details.php?id=60635"}}`),
	}}
	submitRequest := candidateRequest(http.MethodPost, "/api/v2/candidates/55555555-5555-4555-8555-555555555555/retorrent-job", `{"execution_mode":"unsafe"}`, "jobs:write")
	submitRequest.SetPathValue("candidate_id", store.item.ID)
	submitRequest.Header.Set("Idempotency-Key", "invalid-mode-submit")
	submitResponse := httptest.NewRecorder()
	(candidateAPI{store: store, jobs: jobs}).submitRetorrent(submitResponse, submitRequest)
	if submitResponse.Code != http.StatusBadRequest {
		t.Fatalf("submit response = %d/%s", submitResponse.Code, submitResponse.Body.String())
	}
}

func TestSubmitCandidateCancelsJobWhenConcurrentSubmissionWins(t *testing.T) {
	rank := 1
	store := &fakeCandidateStore{markErr: candidates.ErrNotSubmittable, item: candidates.Item{
		ID: "55555555-5555-4555-8555-555555555555", Rank: &rank, Status: candidates.StatusCandidate,
		ExpiresAt: time.Now().Add(time.Hour), TargetSite: "MTEAM",
		Payload: json.RawMessage(`{"ready":true,"source":{"details_url":"https://u2.dmhy.org/details.php?id=60635"}}`),
	}}
	jobs := &createCaptureJobService{job: workflow.Job{
		ID: "66666666-6666-4666-8666-666666666666", Kind: "retorrent", Status: workflow.JobQueued,
		Blockers: json.RawMessage(`[]`), NextActions: json.RawMessage(`[]`), ResumeState: json.RawMessage(`{}`), Summary: json.RawMessage(`{}`), Input: json.RawMessage(`{}`),
	}}
	request := candidateRequest(http.MethodPost, "/api/v2/candidates/55555555-5555-4555-8555-555555555555/retorrent-job", `{}`, "jobs:write")
	request.SetPathValue("candidate_id", store.item.ID)
	request.Header.Set("Idempotency-Key", "concurrent-candidate-submit")
	response := httptest.NewRecorder()
	(candidateAPI{store: store, jobs: jobs}).submitRetorrent(response, request)
	if response.Code != http.StatusConflict || jobs.cancelledJob != jobs.job.ID {
		t.Fatalf("response/cancelled = %d/%s/%s", response.Code, response.Body.String(), jobs.cancelledJob)
	}
}

func TestListDailyCandidatesRedactsPayloadSecrets(t *testing.T) {
	date := time.Date(2026, 8, 8, 0, 0, 0, 0, time.UTC)
	rank := 1
	store := &fakeCandidateStore{items: []candidates.Item{{
		ID: "55555555-5555-4555-8555-555555555555", Status: candidates.StatusCandidate,
		Payload:            json.RawMessage(`{"details_url":"https://example.invalid/details?id=1&passkey=secret-value"}`),
		RecommendationDate: date,
		Rank:               &rank,
		ExpiresAt:          time.Now().Add(time.Hour),
	}}}
	request := candidateRequest(http.MethodGet, "/api/v2/candidates/daily?source=U2&target=MTEAM&date=2026-08-08", "", "jobs:read")
	response := httptest.NewRecorder()
	(candidateAPI{store: store}).listDaily(response, request)
	if response.Code != http.StatusOK || bytes.Contains(response.Body.Bytes(), []byte("secret-value")) {
		t.Fatalf("list response = %d/%s", response.Code, response.Body.String())
	}
	if !bytes.Contains(response.Body.Bytes(), []byte(`/api/v2/candidates/55555555-5555-4555-8555-555555555555/retorrent-job`)) {
		t.Fatalf("list response is missing the safe task entry: %s", response.Body.String())
	}
	if store.listFilter.SourceSite != "U2" || store.listFilter.TargetSite != "MTEAM" || store.listFilter.RecommendationDate == nil || !store.listFilter.RecommendationDate.Equal(date) {
		t.Fatalf("list filter = %#v", store.listFilter)
	}
}
