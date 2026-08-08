package worker

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/torrentmaker"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetUploader struct {
	result  sites.TargetUploadEvidence
	err     error
	request sites.TargetUploadRequest
	target  string
	calls   int
}

func (uploader *fakeTargetUploader) Upload(_ context.Context, target string, request sites.TargetUploadRequest, _ workflow.Actor) (sites.TargetUploadEvidence, error) {
	uploader.calls++
	uploader.target, uploader.request = target, request
	return uploader.result, uploader.err
}

func TestTargetUploadStepRequiresConfirmationBeforeReadOrWrite(t *testing.T) {
	execution, store := targetUploadExecution(t, false)
	duplicates := &fakeTargetDuplicateChecker{}
	uploader := &fakeTargetUploader{}
	recorder := &sequenceArtifactRecorder{}
	_, err := (targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)},
		artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "confirm_upload_required" || duplicates.calls != 0 || uploader.calls != 0 || len(recorder.inputs) != 0 {
		t.Fatalf("confirmation blocker/dependencies = %#v/%#v/%#v/%#v", blocked, duplicates, uploader, recorder.inputs)
	}
}

func TestTargetUploadStepRechecksDuplicateThenPersistsUploadReceipt(t *testing.T) {
	execution, store := targetUploadExecution(t, true)
	duplicates := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, CheckedAt: time.Unix(2, 0).UTC(),
	}}
	uploader := &fakeTargetUploader{result: sites.TargetUploadEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("c", 64),
		TorrentID: "98765", DetailsURL: "https://kp.m-team.cc/details/98765",
		ResponseSHA256: strings.Repeat("e", 64), SubmittedAt: time.Unix(3, 0).UTC(),
	}}
	recorder := &sequenceArtifactRecorder{}
	output, err := (targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)},
		artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if duplicates.calls != 1 || uploader.calls != 1 || uploader.target != "MTEAM" || !uploader.request.Confirmed ||
		uploader.request.DuplicateCheckSHA256 == "" || uploader.request.TorrentSHA256 == "" ||
		len(recorder.inputs) != 2 || recorder.inputs[0].Kind != "preupload_duplicate_check" || recorder.inputs[1].Kind != "target_upload_receipt" {
		t.Fatalf("duplicate/uploader/artifacts = %#v/%#v/%#v", duplicates, uploader, recorder.inputs)
	}
	var result struct {
		Uploaded  bool   `json:"uploaded"`
		Status    string `json:"status"`
		TorrentID string `json:"uploaded_torrent_id"`
		ReceiptID string `json:"upload_receipt_artifact_id"`
	}
	if json.Unmarshal(output, &result) != nil || !result.Uploaded || result.Status != "uploaded" || result.TorrentID != "98765" || result.ReceiptID != "artifact-2" {
		t.Fatalf("upload output = %s", output)
	}
}

func TestTargetUploadStepStopsOnFinalDuplicateAndUnknownOutcome(t *testing.T) {
	execution, store := targetUploadExecution(t, true)
	duplicates := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, Duplicate: true, ResultCount: 1,
		Candidates: []mteam.DuplicateCandidate{{ID: "42", Name: "Existing.Release"}}, CheckedAt: time.Unix(2, 0).UTC(),
	}}
	uploader := &fakeTargetUploader{}
	recorder := &sequenceArtifactRecorder{}
	executor := targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)}, artifacts: store, recorder: recorder,
	}
	_, err := executor.Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_duplicate_detected" || uploader.calls != 0 || len(recorder.inputs) != 1 || recorder.inputs[0].Kind != "preupload_duplicate_check" {
		t.Fatalf("final duplicate blocker/uploader/artifacts = %#v/%#v/%#v", blocked, uploader, recorder.inputs)
	}

	execution, store = targetUploadExecution(t, true)
	duplicates = &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, CheckedAt: time.Unix(2, 0).UTC(),
	}}
	uploader = &fakeTargetUploader{err: sites.NewAdapterError(
		"target_upload_outcome_unknown", "response was lost", false, nil,
	)}
	recorder = &sequenceArtifactRecorder{}
	_, err = (targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)}, artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	blocked = requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_upload_outcome_unknown" || blocked.NextActions[0].Action != "reconcile_target_upload" ||
		len(recorder.inputs) != 1 || blocked.ResumeState["confirm_upload"] != false {
		t.Fatalf("unknown outcome blocker/artifacts = %#v/%#v", blocked, recorder.inputs)
	}
}

func targetUploadExecution(t *testing.T, confirmed bool) (Execution, WorkflowArtifactStore) {
	t.Helper()
	store := mustArtifactStore(t)
	source := targetTorrentMetainfo("https://source.example/announce/passkey", "U2", 3, nil)
	torrentExecution := targetTorrentExecution(t, store, source)
	profiles, err := sites.NewTargetTorrentRegistry(mteam.NewTorrentAdapter())
	if err != nil {
		t.Fatal(err)
	}
	torrentRecorder := &sequenceArtifactRecorder{}
	torrentOutput, err := (targetTorrentExecutor{
		profiles: profiles,
		maker: &fakeTargetTorrentMaker{result: torrentmaker.Result{
			Torrent: targetTorrentMetainfo("https://fake.tracker", "MTEAM", 3, nil),
			Tool:    "mkbrr", Version: "v1.23.0", Verification: "100.00%",
		}},
		artifacts: store, recorder: torrentRecorder,
	}).Execute(context.Background(), torrentExecution)
	if err != nil {
		t.Fatal(err)
	}
	var frozen struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(torrentExecution.Step.InputSnapshot, &frozen); err != nil {
		t.Fatal(err)
	}
	frozen.PreviousSteps["target_torrent"] = torrentOutput
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "target-upload-step", InputSnapshot: mustJSON(map[string]any{
			"job_input": map[string]any{"confirm_upload": confirmed}, "resume_state": map[string]any{},
			"previous_steps": frozen.PreviousSteps,
		})},
		Attempt: workflow.Attempt{ID: "target-upload-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}, store
}

func targetUploadRuleRevision(t *testing.T) rules.Revision {
	t.Helper()
	policy := rules.Policy{
		SchemaVersion: 1,
		Site:          rules.Site{Code: "MTEAM", DisplayName: "M-Team", Roles: []string{"target"}},
		Source:        rules.Source{URL: "https://wiki.m-team.cc/zh-tw/site-rules", Complete: true},
		Automation: rules.Automation{
			Download: true, Upload: true, Retorrent: true, AutoPull: true, AutoUpload: true,
		},
	}
	body, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	return rules.Revision{
		ID: "rule-id", SiteCode: "MTEAM", Status: "approved",
		Fingerprint: strings.Repeat("f", 64), Policy: body,
	}
}
