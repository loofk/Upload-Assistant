package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type previewJobService struct {
	JobService
	job       workflow.Job
	steps     []workflow.Step
	artifacts []workflow.Artifact
	revision  workflow.ReviseTargetPackageInput
}

func (service *previewJobService) GetJob(context.Context, string) (workflow.Job, error) {
	return service.job, nil
}
func (service *previewJobService) ListSteps(context.Context, string) ([]workflow.Step, error) {
	return service.steps, nil
}
func (service *previewJobService) ListArtifacts(context.Context, string) ([]workflow.Artifact, error) {
	return service.artifacts, nil
}
func (service *previewJobService) GetArtifact(_ context.Context, _, id string) (workflow.Artifact, error) {
	for _, artifact := range service.artifacts {
		if artifact.ID == id {
			return artifact, nil
		}
	}
	return workflow.Artifact{}, workflow.ErrNotFound
}
func (service *previewJobService) ReviseTargetPackage(_ context.Context, _ string, input workflow.ReviseTargetPackageInput) (workflow.Job, error) {
	service.revision = input
	service.job.Status = workflow.JobQueued
	service.job.CurrentStep = "target_package"
	return service.job, nil
}

func previewFixture(t *testing.T) (*previewJobService, *artifactReader, string) {
	t.Helper()
	body := []byte(`{"schema_version":1,"target":"MTEAM","adapter":"mteam_api","source":{"tracker":"U2","torrent_id":"1"},"metadata_links":{},"form_fields":{"name":"Fixture","namingProfile":"anime_encode","small_descr":"Fixture","category":405,"standard":1,"anonymous":false},"description":"[quote]review me[/quote]","mediainfo":{"kind":"mediainfo"},"content":{},"evidence":{},"decisions":[],"warnings":[],"naming_profiles":[{"id":"anime_encode","label":"动画 Encode","release_title":{"required":true,"pattern":"^.+$"}}],"manual_review_required":true,"generated_at":"2026-08-10T00:00:00Z"}`)
	digest := sha256.Sum256(body)
	sha := hex.EncodeToString(digest[:])
	jobID := "44444444-4444-4444-8444-444444444444"
	artifactID := "55555555-5555-4555-8555-555555555555"
	service := &previewJobService{
		job:       workflow.Job{ID: jobID, Kind: "retorrent", Status: workflow.JobBlocked, CurrentStep: "target_upload", Blockers: json.RawMessage(`[{"code":"confirm_upload_required"}]`), NextActions: json.RawMessage(`[]`), ResumeState: json.RawMessage(`{}`), Summary: json.RawMessage(`{}`)},
		steps:     []workflow.Step{{Key: "target_package", Status: workflow.StepComplete, OutputSummary: json.RawMessage(`{"package_artifact_id":"` + artifactID + `"}`)}, {Key: "target_upload", Status: workflow.StepBlocked}},
		artifacts: []workflow.Artifact{{ID: artifactID, JobID: jobID, Kind: "target_package", StorageBackend: "local", StoragePath: "jobs/package.json", Filename: "package.json", SizeBytes: int64(len(body)), SHA256: sha, Metadata: json.RawMessage(`{}`), ExpiresAt: time.Now().Add(time.Hour), CreatedAt: time.Now()}},
	}
	return service, &artifactReader{body: body}, sha
}

func TestUploadPreviewReturnsVerifiedReadablePackage(t *testing.T) {
	service, reader, _ := previewFixture(t)
	request := httptest.NewRequest(http.MethodGet, "/preview", nil)
	request.SetPathValue("job_id", service.job.ID)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{UserID: "operator", Role: "admin", TokenScopes: []string{"jobs:read", "audit:read"}}))
	response := httptest.NewRecorder()
	(jobsAPI{service: service, reader: reader}).uploadPreview(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"package_revision":1`) || !strings.Contains(response.Body.String(), "review me") || !strings.Contains(response.Body.String(), `"can_revise":true`) {
		t.Fatalf("preview response = %d/%s", response.Code, response.Body.String())
	}
}

func TestUploadPreviewRevisionQueuesRegenerationAndBindsSHA(t *testing.T) {
	service, reader, sha := previewFixture(t)
	body := `{"expected_package_sha256":"` + sha + `","fields":{"name":"Reviewed","naming_profile":"anime_encode","small_descr":"Reviewed","category":405,"category_evidence":"anime","standard":1,"anonymous":false,"description":"[quote]approved preview[/quote]"}}`
	request := httptest.NewRequest(http.MethodPost, "/revisions", strings.NewReader(body))
	request.SetPathValue("job_id", service.job.ID)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{UserID: "operator", Role: "admin", TokenScopes: []string{"jobs:write", "audit:read"}}))
	response := httptest.NewRecorder()
	(jobsAPI{service: service, reader: reader}).reviseUploadPreview(response, request)
	if response.Code != http.StatusAccepted || service.revision.ExpectedPackageSHA256 != sha || !strings.Contains(string(service.revision.Options), `"description":"[quote]approved preview[/quote]"`) || !strings.Contains(string(service.revision.Options), `"naming_profile":"anime_encode"`) {
		t.Fatalf("revision/response = %#v/%d/%s", service.revision, response.Code, response.Body.String())
	}
}
