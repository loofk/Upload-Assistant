package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestTargetPackageStepVerifiesMaterialsAndPersistsPackage(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	executor := targetPackageExecutorForTest(t, store, recorder, rules.Naming{})
	output, err := executor.Execute(context.Background(), targetPackageExecution(t, store, "U2", map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	if recorder.recorded.Kind != "target_package" || recorder.recorded.SHA256 == "" {
		t.Fatalf("target package artifact = %#v", recorder.recorded)
	}
	var summary struct {
		Prepared          bool           `json:"prepared"`
		Target            string         `json:"target"`
		FormFields        map[string]any `json:"form_fields"`
		PackageArtifactID string         `json:"package_artifact_id"`
	}
	if err := json.Unmarshal(output, &summary); err != nil || !summary.Prepared || summary.Target != "MTEAM" ||
		summary.FormFields["category"] != float64(405) || summary.PackageArtifactID != "artifact-id" {
		t.Fatalf("target package summary/error = %#v/%v", summary, err)
	}
	file, err := store.Open(recorder.recorded.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var prepared sites.PreparedTargetPackage
	if err := json.NewDecoder(file).Decode(&prepared); err != nil || prepared.FormFields["standard"] != float64(1) ||
		!strings.Contains(prepared.Description, "https://i.ibb.co/fixture/image.png") {
		t.Fatalf("stored package/error = %#v/%v", prepared, err)
	}
}

func TestTargetPackageStepBlocksAndResumesUncertainCategory(t *testing.T) {
	store := mustArtifactStore(t)
	executor := targetPackageExecutorForTest(t, store, &fakeArtifactRecorder{}, rules.Naming{})
	execution := targetPackageExecution(t, store, "CHD", map[string]any{})
	_, err := executor.Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if len(blocked.Blockers) != 1 || blocked.Blockers[0].Code != "target_category_required" ||
		blocked.NextActions[0].Action != "provide_target_package_fields" {
		t.Fatalf("category blocker = %#v", blocked)
	}

	execution = targetPackageExecution(t, store, "CHD", map[string]any{
		"category": 419, "category_evidence": "current MTEAM HD movie category",
	})
	if _, err := executor.Execute(context.Background(), execution); err != nil {
		t.Fatalf("resumed target package error = %v", err)
	}
}

func TestTargetPackageStepEnforcesActiveTargetNamingRule(t *testing.T) {
	store := mustArtifactStore(t)
	execution := targetPackageExecution(t, store, "U2", map[string]any{"name": "invalid title"})
	naming := rules.Naming{
		ReleaseTitle: rules.NamingConstraint{Required: true, Pattern: `^Fixture\.Release\.2026\.1080p-[A-Z]+$`, Template: "{title}-{group}"},
		ContentName:  rules.NamingConstraint{Required: true, Pattern: `^release$`, Template: "release"},
	}
	executor := targetPackageExecutorForTest(t, store, &fakeArtifactRecorder{}, naming)
	_, err := executor.Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if len(blocked.Blockers) != 1 || blocked.Blockers[0].Code != "target_release_title_mismatch" {
		t.Fatalf("naming blocker = %#v", blocked)
	}
	var snapshot map[string]any
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		t.Fatal(err)
	}
	resume := snapshot["resume_state"].(map[string]any)
	resume["target_package"] = map[string]any{"name": "Fixture.Release.2026.1080p-GROUP"}
	execution.Step.InputSnapshot = mustJSON(snapshot)
	if _, err := executor.Execute(context.Background(), execution); err != nil {
		t.Fatalf("reviewed naming error = %v", err)
	}
}

func TestTargetPackageStepRequiresAndEnforcesNamingProfile(t *testing.T) {
	store := mustArtifactStore(t)
	naming := rules.Naming{Profiles: []rules.NamingProfile{
		{ID: "anime_encode", Label: "动画 Encode", ReleaseTitle: rules.NamingConstraint{Required: true, Pattern: `^Fixture\.Release\.2026\.1080p-[A-Z]+$`, Template: "英文名 年份 分辨率 参数-小组"}},
		{ID: "anime_web_episode", Label: "动画 WEB 单集", ReleaseTitle: rules.NamingConstraint{Required: true, Pattern: `^Fixture\.Release\.S[0-9]{2}E[0-9]{2}\.1080p-[A-Z]+$`, Template: "英文名 季集 分辨率 参数-小组"}},
	}}
	executor := targetPackageExecutorForTest(t, store, &fakeArtifactRecorder{}, naming)
	execution := targetPackageExecution(t, store, "U2", map[string]any{"name": "Fixture.Release.2026.1080p-GROUP"})
	_, err := executor.Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if len(blocked.Blockers) != 1 || blocked.Blockers[0].Code != "target_naming_profile_required" {
		t.Fatalf("naming profile blocker = %#v", blocked)
	}

	var snapshot map[string]any
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		t.Fatal(err)
	}
	resume := snapshot["resume_state"].(map[string]any)
	resume["target_package"] = map[string]any{
		"name": "Fixture.Release.2026.1080p-GROUP", "naming_profile": "anime_encode",
	}
	execution.Step.InputSnapshot = mustJSON(snapshot)
	if _, err := executor.Execute(context.Background(), execution); err != nil {
		t.Fatalf("profiled naming error = %v", err)
	}
}

func TestTargetPackageStepRejectsTamperedMediaArtifact(t *testing.T) {
	store := mustArtifactStore(t)
	execution := targetPackageExecution(t, store, "U2", map[string]any{})
	var snapshot map[string]any
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		t.Fatal(err)
	}
	previous := snapshot["previous_steps"].(map[string]any)
	mediaOutput := previous["media_info"].(map[string]any)
	mediaOutput["artifact_sha256"] = strings.Repeat("0", 64)
	execution.Step.InputSnapshot = mustJSON(snapshot)
	_, err := targetPackageExecutorForTest(t, store, &fakeArtifactRecorder{}, rules.Naming{}).Execute(context.Background(), execution)
	var blocked *BlockError
	if !errors.As(err, &blocked) || blocked.Code != "step_input_snapshot_invalid" {
		t.Fatalf("tampered media blocker = %#v", blocked)
	}
}

func TestTargetPackageStepAcceptsVerifiedBDInfoArtifact(t *testing.T) {
	store := mustArtifactStore(t)
	execution := targetPackageExecution(t, store, "U2", map[string]any{})
	report := []byte("DISC INFO:\nDisc Title: Fixture\nVideo: MPEG-4 AVC Video / 1080p / 23.976 fps")
	file, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "media-step", AttemptID: "bdinfo-attempt",
	}, "bdinfo.txt", bytes.NewReader(report))
	if err != nil {
		t.Fatal(err)
	}
	var snapshot map[string]any
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		t.Fatal(err)
	}
	mediaOutput := snapshot["previous_steps"].(map[string]any)["media_info"].(map[string]any)
	mediaOutput["kind"] = "bdinfo"
	mediaOutput["tool"] = "bdinfo"
	mediaOutput["document_format"] = "text"
	mediaOutput["artifact_sha256"] = file.SHA256
	mediaOutput["artifact_storage_path"] = file.RelativePath
	execution.Step.InputSnapshot = mustJSON(snapshot)

	recorder := &fakeArtifactRecorder{}
	if _, err := targetPackageExecutorForTest(t, store, recorder, rules.Naming{}).Execute(context.Background(), execution); err != nil {
		t.Fatal(err)
	}
	stored, err := store.Open(recorder.recorded.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer stored.Close()
	var prepared sites.PreparedTargetPackage
	var mediaText string
	if err := json.NewDecoder(stored).Decode(&prepared); err != nil {
		t.Fatal(err)
	}
	decodeErr := json.Unmarshal(prepared.MediaInfo, &mediaText)
	if decodeErr != nil || mediaText != string(report) || !strings.Contains(prepared.Description, "[b]BDInfo[/b]") {
		t.Fatalf("stored BDInfo package/error = %#v/%v", prepared, decodeErr)
	}
}

func TestTargetPackageStepVerifiesV2MetadataArtifacts(t *testing.T) {
	store := mustArtifactStore(t)
	execution := targetPackageExecution(t, store, "U2", map[string]any{})
	identity := metadataIdentity{Title: "Fixture.Release.2026.1080p", IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie", DoubanID: "1292052", AniDBID: "3456"}
	description := "[b]Fixture PTGen[/b]"
	document := metadataPTGenDocument{
		SchemaVersion: 1, Source: "provider", Provider: "ptgen-main", Adapter: "ptgen", Identity: identity,
		Description: description, DescriptionSHA256: sha256Hex([]byte(description)),
		ConfigurationSHA256: strings.Repeat("6", 64), QuerySHA256: strings.Repeat("7", 64), GeneratedAt: time.Unix(2, 0).UTC(),
	}
	body, _ := json.Marshal(document)
	file, err := store.Write(context.Background(), artifacts.Scope{JobID: "job-id", StepID: "ptgen-step", AttemptID: "ptgen-attempt"}, "metadata-ptgen.json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var snapshot map[string]any
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		t.Fatal(err)
	}
	previous := snapshot["previous_steps"].(map[string]any)
	tmdbDocument := metadataTMDbDocument{
		SchemaVersion: 1, Source: "provider", Provider: "tmdb-main", Adapter: "tmdb", Identity: identity,
		ConfigurationSHA256: strings.Repeat("4", 64), QuerySHA256: strings.Repeat("8", 64), GeneratedAt: time.Unix(1, 0).UTC(),
	}
	tmdbBody, _ := json.Marshal(tmdbDocument)
	tmdbFile, err := store.Write(context.Background(), artifacts.Scope{JobID: "job-id", StepID: "tmdb-step", AttemptID: "tmdb-attempt"}, "metadata-tmdb.json", bytes.NewReader(tmdbBody))
	if err != nil {
		t.Fatal(err)
	}
	previous["metadata_tmdb"] = map[string]any{
		"resolved": true, "identity": identity, "links": metadataLinks(identity), "provider": "tmdb-main", "adapter": "tmdb",
		"configuration_sha256": strings.Repeat("4", 64), "query_sha256": strings.Repeat("8", 64),
		"artifact_id": "tmdb-artifact", "artifact_sha256": tmdbFile.SHA256, "artifact_storage_path": tmdbFile.RelativePath,
	}
	previous["metadata_ptgen"] = map[string]any{
		"resolved": true, "identity": identity, "provider": "ptgen-main", "adapter": "ptgen",
		"configuration_sha256": strings.Repeat("6", 64), "query_sha256": strings.Repeat("7", 64), "description_sha256": document.DescriptionSHA256,
		"description_size_bytes": len([]byte(description)), "artifact_id": "ptgen-artifact", "artifact_sha256": file.SHA256, "artifact_storage_path": file.RelativePath,
	}
	execution.Step.InputSnapshot = mustJSON(snapshot)
	recorder := &fakeArtifactRecorder{}
	if _, err := targetPackageExecutorForTest(t, store, recorder, rules.Naming{}).Execute(context.Background(), execution); err != nil {
		t.Fatal(err)
	}
	stored, err := store.Open(recorder.recorded.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer stored.Close()
	var prepared sites.PreparedTargetPackage
	if json.NewDecoder(stored).Decode(&prepared) != nil || !strings.Contains(prepared.Description, "Fixture PTGen") || prepared.FormFields["imdb"] == nil || prepared.FormFields["douban"] == nil {
		t.Fatalf("prepared package = %#v", prepared)
	}
}

func targetPackageRegistry(t *testing.T) *sites.TargetPackageRegistry {
	t.Helper()
	registry, err := sites.NewTargetPackageRegistry(mteam.NewPackageAdapter())
	if err != nil {
		t.Fatal(err)
	}
	return registry
}

func targetPackageExecution(t *testing.T, store *artifacts.LocalStore, source string, resumeOptions map[string]any) Execution {
	t.Helper()
	descriptionBody := []byte(`<p>Fixture source description</p>`)
	descriptionFile, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "source-step", AttemptID: "source-attempt",
	}, "source-description.html", bytes.NewReader(descriptionBody))
	if err != nil {
		t.Fatal(err)
	}
	mediaBody := []byte(`{"media":{"track":[{"@type":"Video","Width":"1920","Height":"1080"}]}}`)
	mediaFile, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "media-step", AttemptID: "media-attempt",
	}, "mediainfo.json", bytes.NewReader(mediaBody))
	if err != nil {
		t.Fatal(err)
	}
	info := sites.SourceInfo{
		Tracker: source, TorrentID: "60635", Name: "Fixture.Release.2026.1080p",
		DetailsURL: "https://source.invalid/details.php?id=60635",
	}
	links := map[string]string{"imdb": "https://www.imdb.com/title/tt1234567/"}
	if source == "U2" {
		info.AniDBID = "3456"
		links["anidb"] = "https://anidb.net/anime/3456"
	}
	receipt := imageUploadReceipt{
		Index: 1, Timestamp: 50,
		Source: screenshotArtifactInput{Index: 1, Filename: "screenshot-01.png", MIMEType: "image/png", SHA256: strings.Repeat("b", 64)},
		Host:   imagehosts.HostSnapshot{ID: "host-id", Name: "default", Adapter: "imgbb", ConfigSHA256: "config-sha", ConfigurationTime: time.Unix(1, 0).UTC()},
		Upload: imagehosts.UploadEvidence{
			ImageHostID: "host-id", ImageHostName: "default", Adapter: "imgbb",
			ConfigSHA256: "config-sha", ConfigurationTime: time.Unix(1, 0).UTC(),
			Result: imagehosts.UploadResult{URL: "https://i.ibb.co/fixture/image.png", ViewerURL: "https://ibb.co/fixture"},
		},
		ReceiptID: "receipt-id", ReceiptSHA: strings.Repeat("c", 64),
	}
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "target-package-step", InputSnapshot: mustJSON(map[string]any{
			"job_input":    map[string]any{"target_package": map[string]any{}},
			"resume_state": map[string]any{"target_package": resumeOptions},
			"previous_steps": map[string]any{
				"source_parse": map[string]any{"source": sites.SourceReference{Tracker: source, TorrentID: "60635"}, "target": "MTEAM"},
				"source_inspect": map[string]any{
					"source_info": info,
					"description_artifact": map[string]any{
						"artifact_id": "description-id", "storage_path": descriptionFile.RelativePath,
						"sha256": descriptionFile.SHA256, "size_bytes": descriptionFile.SizeBytes,
					},
				},
				"source_rules":   map[string]any{"rule_revision_id": "rule-id", "fingerprint": strings.Repeat("d", 64)},
				"source_torrent": map[string]any{"artifact_id": "torrent-id", "sha256": strings.Repeat("e", 64), "hashes": map[string]any{"v1_sha1": strings.Repeat("f", 40)}},
				"content_resolve": map[string]any{
					"resolved": true, "downloader_name": "box", "torrent_hash": strings.Repeat("f", 40),
					"local_root": "/downloads/release", "file_count": 1, "total_size_bytes": 13,
					"manifest_artifact_id": "manifest-id", "manifest_sha256": strings.Repeat("a", 64), "manifest_storage_path": "manifest/path.json",
				},
				"metadata": map[string]any{
					"identity": map[string]any{"title": info.Name, "imdb_id": "tt1234567", "anidb_id": info.AniDBID},
					"links":    links, "metadata_artifact_id": "metadata-id", "metadata_sha256": strings.Repeat("1", 64), "metadata_storage_path": "metadata/path.json",
				},
				"media_info": map[string]any{
					"kind": "mediainfo", "tool": "mediainfo", "version": "fixture", "artifact_id": "media-id",
					"artifact_sha256": mediaFile.SHA256, "artifact_storage_path": mediaFile.RelativePath,
				},
				"image_upload": map[string]any{"uploaded": true, "receipts": []imageUploadReceipt{receipt}},
			},
		})},
		Attempt: workflow.Attempt{ID: "target-package-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}

type targetPackageRuleProvider struct {
	revision rules.Revision
}

func (provider targetPackageRuleProvider) Active(_ context.Context, siteCode string) (rules.Revision, error) {
	if siteCode != provider.revision.SiteCode {
		return rules.Revision{}, rules.ErrNotFound
	}
	return provider.revision, nil
}

func targetPackageExecutorForTest(t *testing.T, store *artifacts.LocalStore, recorder ArtifactRecorder, naming rules.Naming) targetPackageExecutor {
	t.Helper()
	policy, err := json.Marshal(rules.Policy{
		SchemaVersion: 2,
		Site:          rules.Site{Code: "MTEAM", DisplayName: "M-Team", Roles: []string{"target"}},
		Source:        rules.Source{Complete: true},
		Access:        rules.Access{ServiceAccess: "undetermined", SearchAccess: "undetermined"},
		Naming:        naming,
	})
	if err != nil {
		t.Fatal(err)
	}
	return targetPackageExecutor{
		provider: targetPackageRegistry(t), artifacts: store, recorder: recorder,
		rules: targetPackageRuleProvider{revision: rules.Revision{
			ID: "target-rule-id", SiteCode: "MTEAM", Status: "approved",
			Fingerprint: strings.Repeat("c", 64), Policy: policy,
		}},
	}
}
