package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetTorrentDownloader struct {
	result  sites.DownloadedTargetTorrent
	err     error
	request sites.TargetTorrentDownloadRequest
	target  string
	calls   int
}

func (downloader *fakeTargetTorrentDownloader) DownloadUploadedTorrent(_ context.Context, target string, request sites.TargetTorrentDownloadRequest, _ workflow.Actor) (sites.DownloadedTargetTorrent, error) {
	downloader.calls++
	downloader.target, downloader.request = target, request
	return downloader.result, downloader.err
}

func TestTargetTorrentDownloadStepPersistsRedactedVerifiedEvidence(t *testing.T) {
	execution, store, submitted := targetTorrentDownloadExecution(t)
	downloaded := targetTorrentMetainfo("https://tracker.m-team.cc/announce/secret-passkey", "MTEAM", 3, nil)
	downloader := &fakeTargetTorrentDownloader{result: targetTorrentDownloadResult(t, downloaded)}
	recorder := &sequenceArtifactRecorder{}
	output, err := (targetTorrentDownloadExecutor{downloader: downloader, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if downloader.calls != 1 || downloader.target != "MTEAM" || downloader.request.TorrentID != "98765" ||
		downloader.request.UploadReceiptSHA256 == "" || downloader.request.SubmittedTorrentSHA256 != sha256String(submitted) ||
		len(recorder.inputs) != 2 || recorder.inputs[0].Kind != "target_downloaded_torrent" || recorder.inputs[1].Kind != "target_torrent_download_receipt" {
		t.Fatalf("downloader/artifacts = %#v/%#v", downloader, recorder.inputs)
	}
	var result struct {
		Downloaded bool   `json:"downloaded"`
		Verified   bool   `json:"verified"`
		Status     string `json:"status"`
		ArtifactID string `json:"target_torrent_artifact_id"`
		ReceiptID  string `json:"receipt_artifact_id"`
	}
	if json.Unmarshal(output, &result) != nil || !result.Downloaded || !result.Verified || result.Status != "ready_for_injection" ||
		result.ArtifactID != "artifact-1" || result.ReceiptID != "artifact-2" {
		t.Fatalf("target torrent download output = %s", output)
	}
	receiptFile, err := store.Open(recorder.inputs[1].StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	receiptBody, _ := io.ReadAll(receiptFile)
	receiptFile.Close()
	if bytes.Contains(receiptBody, []byte("secret-passkey")) || bytes.Contains(receiptBody, []byte("tracker.m-team.cc")) ||
		!bytes.Contains(receiptBody, []byte(`"signed_download_url_sha256"`)) {
		t.Fatalf("target download receipt redaction = %s", receiptBody)
	}
}

func TestTargetTorrentDownloadStepRejectsPayloadMismatchBeforeArtifactWrite(t *testing.T) {
	execution, store, _ := targetTorrentDownloadExecution(t)
	mismatch := targetTorrentMetainfo("https://tracker.m-team.cc/announce/passkey", "MTEAM", 4, nil)
	downloader := &fakeTargetTorrentDownloader{result: targetTorrentDownloadResult(t, mismatch)}
	recorder := &sequenceArtifactRecorder{}
	_, err := (targetTorrentDownloadExecutor{downloader: downloader, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_torrent_download_evidence_invalid" || blocked.NextActions[0].Action != "reconcile_uploaded_torrent" || len(recorder.inputs) != 0 {
		t.Fatalf("mismatch blocker/artifacts = %#v/%#v", blocked, recorder.inputs)
	}
}

func TestTargetTorrentDownloadStepMapsTemporaryAdapterFailureToSafeRetry(t *testing.T) {
	execution, store, _ := targetTorrentDownloadExecution(t)
	downloader := &fakeTargetTorrentDownloader{err: sites.NewAdapterError(
		"target_torrent_download_failed", "temporary MTEAM failure", true, nil,
	)}
	_, err := (targetTorrentDownloadExecutor{downloader: downloader, artifacts: store, recorder: &sequenceArtifactRecorder{}}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_torrent_download_failed" || blocked.NextActions[0].Action != "retry_target_torrent_download" {
		t.Fatalf("temporary download blocker = %#v", blocked)
	}
}

func targetTorrentDownloadExecution(t *testing.T) (Execution, WorkflowArtifactStore, []byte) {
	t.Helper()
	uploadExecution, store := targetUploadExecution(t, true)
	duplicates := &fakeTargetDuplicateChecker{result: sites.TargetDuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("d", 64),
		Query: sites.TargetDuplicateQuery{IMDbID: "tt1234567"}, CheckedAt: time.Unix(2, 0).UTC(),
	}}
	uploader := &fakeTargetUploader{result: sites.TargetUploadEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("c", 64),
		TorrentID: "98765", DetailsURL: "https://kp.m-team.cc/details/98765",
		ResponseSHA256: strings.Repeat("e", 64), SubmittedAt: time.Unix(3, 0).UTC(),
	}}
	uploadOutput, err := (targetUploadExecutor{
		uploader: uploader, duplicates: duplicates, rules: fakeRuleProvider{revision: targetUploadRuleRevision(t)},
		artifacts: store, recorder: &sequenceArtifactRecorder{},
	}).Execute(context.Background(), uploadExecution)
	if err != nil {
		t.Fatal(err)
	}
	var frozen struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(uploadExecution.Step.InputSnapshot, &frozen); err != nil {
		t.Fatal(err)
	}
	frozen.PreviousSteps["target_upload"] = uploadOutput
	var submittedOutput struct {
		StoragePath string `json:"target_torrent_storage_path"`
	}
	if err := json.Unmarshal(frozen.PreviousSteps["target_torrent"], &submittedOutput); err != nil {
		t.Fatal(err)
	}
	file, err := store.Open(submittedOutput.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	submitted, err := io.ReadAll(file)
	file.Close()
	if err != nil {
		t.Fatal(err)
	}
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "target-download-step", InputSnapshot: mustJSON(map[string]any{
			"previous_steps": frozen.PreviousSteps,
		})},
		Attempt: workflow.Attempt{ID: "target-download-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}, store, submitted
}

func targetTorrentDownloadResult(t *testing.T, torrent []byte) sites.DownloadedTargetTorrent {
	t.Helper()
	inspection, err := torrentmeta.Inspect(torrent)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(torrent)
	announce := sha256.Sum256([]byte(inspection.Announce))
	return sites.DownloadedTargetTorrent{
		Bytes: torrent,
		Evidence: sites.TargetTorrentDownloadEvidence{
			SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("c", 64),
			TorrentID: "98765", Filename: "mteam-98765.torrent", ContentType: "application/x-bittorrent",
			SizeBytes: int64(len(torrent)), SHA256: hex.EncodeToString(digest[:]), Hashes: inspection.Hashes,
			ContentFingerprint: inspection.ContentFingerprint, AnnounceSHA256: hex.EncodeToString(announce[:]),
			TokenResponseSHA256: strings.Repeat("a", 64), SignedDownloadURLSHA256: strings.Repeat("b", 64),
			DownloadedAt: time.Unix(4, 0).UTC(),
		},
	}
}
