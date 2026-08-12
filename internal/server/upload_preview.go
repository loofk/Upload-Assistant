package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxUploadPreviewBytes = 24 << 20

var uploadPreviewNamingProfilePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

type reviseUploadPreviewRequest struct {
	ExpectedPackageSHA256 string `json:"expected_package_sha256"`
	Fields                struct {
		Name             string `json:"name"`
		NamingProfile    string `json:"naming_profile,omitempty"`
		SmallDescription string `json:"small_descr"`
		Category         int    `json:"category"`
		CategoryEvidence string `json:"category_evidence,omitempty"`
		Standard         int    `json:"standard"`
		Anonymous        bool   `json:"anonymous"`
		Description      string `json:"description"`
	} `json:"fields"`
}

func (a jobsAPI) uploadPreview(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:read")
	if !ok {
		return
	}
	if !principal.HasScope("audit:read") {
		writeProblem(w, http.StatusForbidden, "permission_denied", "upload preview requires audit:read")
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, steps, artifact, prepared, revision, err := a.loadUploadPreview(r, id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": job.Status, "job_id": id,
		"package_revision": revision, "package_artifact": redactArtifacts([]workflow.Artifact{artifact})[0],
		"package": prepared, "can_revise": canReviseUploadPackage(job, steps),
		"blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions),
	})
}

func (a jobsAPI) reviseUploadPreview(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	if !principal.HasScope("audit:read") {
		writeProblem(w, http.StatusForbidden, "permission_denied", "upload preview revision requires audit:read")
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	var request reviseUploadPreviewRequest
	if err := decodeJSONLimit(w, r, &request, 5<<20); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	request.ExpectedPackageSHA256 = strings.ToLower(strings.TrimSpace(request.ExpectedPackageSHA256))
	request.Fields.Name = strings.TrimSpace(request.Fields.Name)
	request.Fields.NamingProfile = strings.TrimSpace(request.Fields.NamingProfile)
	request.Fields.SmallDescription = strings.TrimSpace(request.Fields.SmallDescription)
	request.Fields.CategoryEvidence = strings.TrimSpace(request.Fields.CategoryEvidence)
	request.Fields.Description = strings.TrimSpace(request.Fields.Description)
	if !validUploadPreviewFields(request) {
		writeProblem(w, http.StatusBadRequest, "upload_preview_fields_invalid", "name, small_descr, category, standard, or category_evidence is outside the supported MTEAM bounds")
		return
	}
	options, _ := json.Marshal(map[string]any{
		"name": request.Fields.Name, "naming_profile": request.Fields.NamingProfile, "small_descr": request.Fields.SmallDescription,
		"category": request.Fields.Category, "category_evidence": request.Fields.CategoryEvidence,
		"standard": request.Fields.Standard, "anonymous": request.Fields.Anonymous,
		"description": request.Fields.Description,
	})
	job, err := a.service.ReviseTargetPackage(r.Context(), id, workflow.ReviseTargetPackageInput{
		ExpectedPackageSHA256: request.ExpectedPackageSHA256, Options: options,
		Actor: workflow.Actor{Type: "user", ID: principal.UserID},
	})
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]any{
		"ok": true, "status": job.Status, "job_id": job.ID, "current_step": job.CurrentStep,
		"previous_package_sha256": request.ExpectedPackageSHA256,
		"summary":                 "已保留旧发布包并排队生成新版本；live 上传确认已重置。",
		"job":                     redactJob(job), "blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions),
	})
}

func (a jobsAPI) loadUploadPreview(r *http.Request, jobID string) (workflow.Job, []workflow.Step, workflow.Artifact, sites.PreparedTargetPackage, int, error) {
	job, err := a.service.GetJob(r.Context(), jobID)
	if err != nil {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, err
	}
	steps, err := a.service.ListSteps(r.Context(), jobID)
	if err != nil {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, err
	}
	artifactID := ""
	for _, step := range steps {
		if step.Key != "target_package" || step.Status != workflow.StepComplete {
			continue
		}
		var output struct {
			ArtifactID string `json:"package_artifact_id"`
		}
		if json.Unmarshal(step.OutputSummary, &output) == nil {
			artifactID = output.ArtifactID
		}
	}
	if artifactID == "" {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, workflow.ErrNotFound
	}
	artifact, err := a.service.GetArtifact(r.Context(), jobID, artifactID)
	if err != nil {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, err
	}
	if artifact.Kind != "target_package" || artifact.StorageBackend != "local" || artifact.SizeBytes <= 0 || artifact.SizeBytes > maxUploadPreviewBytes || a.reader == nil {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, workflow.ErrNotFound
	}
	body, err := a.reader.Read(r.Context(), artifact.StoragePath, maxUploadPreviewBytes)
	if err != nil || int64(len(body)) != artifact.SizeBytes {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, workflow.ErrNotFound
	}
	digest := sha256.Sum256(body)
	if !strings.EqualFold(hex.EncodeToString(digest[:]), artifact.SHA256) {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, workflow.ErrConflict
	}
	var prepared sites.PreparedTargetPackage
	if json.Unmarshal(body, &prepared) != nil || prepared.Target == "" || prepared.Adapter == "" {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, workflow.ErrConflict
	}
	artifacts, err := a.service.ListArtifacts(r.Context(), jobID)
	if err != nil {
		return workflow.Job{}, nil, workflow.Artifact{}, sites.PreparedTargetPackage{}, 0, err
	}
	revision := 0
	for _, item := range artifacts {
		if item.Kind == "target_package" {
			revision++
		}
	}
	return job, steps, artifact, prepared, revision, nil
}

func validUploadPreviewFields(request reviseUploadPreviewRequest) bool {
	if request.Fields.Name == "" || request.Fields.SmallDescription == "" ||
		utf8.RuneCountInString(request.Fields.Name) > 255 || utf8.RuneCountInString(request.Fields.SmallDescription) > 255 ||
		(request.Fields.NamingProfile != "" && !uploadPreviewNamingProfilePattern.MatchString(request.Fields.NamingProfile)) ||
		request.Fields.Category < 1 || request.Fields.Category > 100_000 || utf8.RuneCountInString(request.Fields.CategoryEvidence) > 500 ||
		request.Fields.Description == "" || utf8.RuneCountInString(request.Fields.Description) > 1_000_000 || strings.IndexByte(request.Fields.Description, 0) >= 0 {
		return false
	}
	switch request.Fields.Standard {
	case 1, 2, 3, 5, 6, 7:
		return true
	default:
		return false
	}
}

func canReviseUploadPackage(job workflow.Job, steps []workflow.Step) bool {
	if job.Kind != "retorrent" || (job.Status != workflow.JobPaused && job.Status != workflow.JobBlocked && job.Status != workflow.JobFailed) {
		return false
	}
	for _, step := range steps {
		if step.Key == "target_upload" && step.Status == workflow.StepComplete {
			return false
		}
	}
	var blockers []struct {
		Code string `json:"code"`
	}
	_ = json.Unmarshal(job.Blockers, &blockers)
	for _, blocker := range blockers {
		if strings.HasSuffix(blocker.Code, "_outcome_unknown") || strings.Contains(blocker.Code, "requires_reconciliation") {
			return false
		}
	}
	return true
}
