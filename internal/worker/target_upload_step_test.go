package worker

import (
	"context"
	"encoding/json"
	"errors"
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

type failingTargetArtifactRecorder struct {
	inputs []workflow.RegisterArtifactInput
	failAt int
}

func (recorder *failingTargetArtifactRecorder) RegisterArtifact(_ context.Context, input workflow.RegisterArtifactInput) (workflow.Artifact, error) {
	recorder.inputs = append(recorder.inputs, input)
	if len(recorder.inputs) == recorder.failAt {
		return workflow.Artifact{}, errors.New("fixture artifact catalog unavailable")
	}
	return workflow.Artifact{
		ID: "artifact-1", JobID: input.JobID, StepID: input.StepID, AttemptID: input.AttemptID,
		Kind: input.Kind, StoragePath: input.StoragePath, Filename: input.Filename, SHA256: input.SHA256,
		SizeBytes: input.SizeBytes,
	}, nil
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

func TestTargetUploadStepTreatsPostWriteLocalFailureAsUnknownOutcome(t *testing.T) {
	execution, store := targetUploadExecution(t, true)
	duplicateEvidence := mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, CheckedAt: time.Unix(2, 0).UTC(),
	}
	uploadEvidence := sites.TargetUploadEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("c", 64),
		TorrentID: "98765", DetailsURL: "https://kp.m-team.cc/details/98765",
		ResponseSHA256: strings.Repeat("e", 64), SubmittedAt: time.Unix(3, 0).UTC(),
	}
	recorder := &failingTargetArtifactRecorder{failAt: 2}
	_, err := (targetUploadExecutor{
		uploader: &fakeTargetUploader{result: uploadEvidence}, duplicates: &fakeTargetDuplicateChecker{result: duplicateEvidence},
		rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)}, artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_upload_outcome_unknown" || blocked.NextActions[0].Action != "reconcile_target_upload" ||
		blocked.ResumeState["confirm_upload"] != false || len(recorder.inputs) != 2 {
		t.Fatalf("post-write persistence blocker/artifacts = %#v/%#v", blocked, recorder.inputs)
	}

	execution, store = targetUploadExecution(t, true)
	_, err = (targetUploadExecutor{
		uploader:   &fakeTargetUploader{result: sites.TargetUploadEvidence{SiteCode: "MTEAM", Adapter: "mteam_api"}},
		duplicates: &fakeTargetDuplicateChecker{result: duplicateEvidence}, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)},
		artifacts: store, recorder: &sequenceArtifactRecorder{},
	}).Execute(context.Background(), execution)
	blocked = requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_upload_outcome_unknown" {
		t.Fatalf("invalid post-write evidence blocker = %#v", blocked)
	}
}

func TestTargetUploadStepRecoversVerifiedUploadWithoutSecondWrite(t *testing.T) {
	execution, store := targetUploadExecution(t, false)
	execution = targetUploadRecoveryExecution(t, execution, "98765")
	duplicates := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, Duplicate: true, ResultCount: 1,
		Candidates: []mteam.DuplicateCandidate{{ID: "98765", Name: "Recovered.Release"}}, CheckedAt: time.Unix(4, 0).UTC(),
	}}
	uploader := &fakeTargetUploader{}
	recorder := &sequenceArtifactRecorder{}
	output, err := (targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)},
		artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Uploaded  bool   `json:"uploaded"`
		Recovered bool   `json:"recovered"`
		TorrentID string `json:"uploaded_torrent_id"`
	}
	if json.Unmarshal(output, &result) != nil || !result.Uploaded || !result.Recovered || result.TorrentID != "98765" ||
		duplicates.calls != 1 || uploader.calls != 0 || len(recorder.inputs) != 2 ||
		recorder.inputs[0].Kind != "preupload_duplicate_check" || recorder.inputs[1].Kind != "target_upload_receipt" {
		t.Fatalf("recovered output/dependencies/artifacts = %s/%#v/%#v/%#v", output, duplicates, uploader, recorder.inputs)
	}
	receiptFile, err := store.Open(recorder.inputs[1].StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer receiptFile.Close()
	var receipt targetUploadReceipt
	if json.NewDecoder(receiptFile).Decode(&receipt) != nil || receipt.Reconciliation == nil ||
		!receipt.Reconciliation.Recovered || receipt.Reconciliation.AttemptID != "original-upload-attempt" ||
		receipt.Reconciliation.EvidenceSHA256 != strings.Repeat("e", 64) {
		t.Fatalf("recovery receipt = %#v", receipt.Reconciliation)
	}

	execution, store = targetUploadExecution(t, false)
	execution = targetUploadRecoveryExecution(t, execution, "98765")
	duplicates = &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, Duplicate: true, ResultCount: 1,
		Candidates: []mteam.DuplicateCandidate{{ID: "42", Name: "Another.Release"}}, CheckedAt: time.Unix(4, 0).UTC(),
	}}
	uploader = &fakeTargetUploader{}
	_, err = (targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)},
		artifacts: store, recorder: &sequenceArtifactRecorder{},
	}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_reconciliation_candidate_not_found" || uploader.calls != 0 {
		t.Fatalf("mismatched reconciliation blocker/uploader = %#v/%#v", blocked, uploader)
	}
}

func targetUploadRecoveryExecution(t *testing.T, execution Execution, torrentID string) Execution {
	t.Helper()
	var snapshot struct {
		JobInput      map[string]any             `json:"job_input"`
		ResumeState   map[string]any             `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		t.Fatal(err)
	}
	var targetTorrent struct {
		SHA256 string `json:"target_torrent_sha256"`
	}
	if json.Unmarshal(snapshot.PreviousSteps["target_torrent"], &targetTorrent) != nil || targetTorrent.SHA256 == "" {
		t.Fatal("target torrent evidence is missing")
	}
	snapshot.ResumeState = map[string]any{
		"confirm_upload": false,
		"reconciliation": map[string]any{
			"blocker_code": "target_upload_outcome_unknown", "attempt_id": "original-upload-attempt",
			"decision": "verified_uploaded", "confirmed": true, "evidence_sha256": strings.Repeat("e", 64),
			"observed_at": "2026-08-08T12:00:00Z", "observed_torrent_id": torrentID,
			"submitted_torrent_sha256": targetTorrent.SHA256,
		},
	}
	execution.Step.InputSnapshot = mustJSON(snapshot)
	return execution
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
