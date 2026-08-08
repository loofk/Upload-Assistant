package server

import (
	"context"
	"encoding/json"
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

func (service *listJobService) ListJobs(_ context.Context, filter workflow.ListJobsFilter) (workflow.JobPage, error) {
	service.filter = filter
	return service.page, nil
}

func TestListJobsUsesStableCursorAndFilters(t *testing.T) {
	createdAt := time.Date(2026, time.August, 8, 1, 2, 3, 4, time.UTC)
	job := workflow.Job{
		ID: "44444444-4444-4444-8444-444444444444", Kind: "retorrent", Status: workflow.JobBlocked,
		ExecutionMode: workflow.ExecutionStep, Input: json.RawMessage(`{}`), Blockers: json.RawMessage(`[]`),
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
