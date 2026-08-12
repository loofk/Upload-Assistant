package clientcli

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const fixtureToken = "ua_fixture_token_value_that_is_long_enough"

func TestRetorrentCreateSendsExplicitSafetyControls(t *testing.T) {
	sourceFingerprint := strings.Repeat("a", 64)
	targetFingerprint := strings.Repeat("b", 64)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/api/v2/jobs" || request.Header.Get("Authorization") != "Bearer "+fixtureToken {
			t.Fatalf("request = %s %s auth=%q", request.Method, request.URL.Path, request.Header.Get("Authorization"))
		}
		if !strings.HasPrefix(request.Header.Get("Idempotency-Key"), "cli-") {
			t.Fatalf("idempotency key = %q", request.Header.Get("Idempotency-Key"))
		}
		var payload struct {
			Kind          string `json:"kind"`
			ExecutionMode string `json:"execution_mode"`
			Input         struct {
				SourceURL     string `json:"source_url"`
				Target        string `json:"target"`
				ConfirmUpload bool   `json:"confirm_upload"`
				AcceptRules   map[string]struct {
					Fingerprint string                    `json:"fingerprint"`
					Accepted    bool                      `json:"accepted"`
					Obligations map[string]map[string]any `json:"obligations"`
				} `json:"accept_rules"`
				Downloader struct {
					Name     string   `json:"name"`
					SavePath string   `json:"save_path"`
					Tags     []string `json:"tags"`
				} `json:"downloader"`
				MetadataProviders struct {
					TMDb  string `json:"tmdb"`
					PTGen string `json:"ptgen"`
				} `json:"metadata_providers"`
			} `json:"input"`
		}
		body, _ := io.ReadAll(request.Body)
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatal(err)
		}
		if payload.Kind != "retorrent" || payload.ExecutionMode != "step" || payload.Input.SourceURL == "" || payload.Input.Target != "MTEAM" || !payload.Input.ConfirmUpload {
			t.Fatalf("payload = %#v", payload)
		}
		if !payload.Input.AcceptRules["U2"].Accepted || payload.Input.AcceptRules["MTEAM"].Fingerprint != targetFingerprint || payload.Input.AcceptRules["MTEAM"].Obligations["manual-upload"]["evidence"] != "reviewed-by-user" {
			t.Fatalf("acceptance = %#v", payload.Input.AcceptRules)
		}
		if payload.Input.Downloader.Name != "box" || payload.Input.Downloader.SavePath != "/downloads" || len(payload.Input.Downloader.Tags) != 2 {
			t.Fatalf("downloader = %#v", payload.Input.Downloader)
		}
		if payload.Input.MetadataProviders.TMDb != "tmdb-main" || payload.Input.MetadataProviders.PTGen != "ptgen-main" {
			t.Fatalf("metadata providers = %#v", payload.Input.MetadataProviders)
		}
		response.Header().Set("Content-Type", "application/json")
		response.WriteHeader(http.StatusAccepted)
		_, _ = response.Write([]byte(`{"ok":true,"status":"queued","job_id":"00000000-0000-0000-0000-000000000001"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "retorrent", "create",
		"--source-url", "https://u2.dmhy.org/details.php?id=60635", "--target", "mteam",
		"--downloader", "box", "--save-path", "/downloads", "--tags", "source,retorrent",
		"--tmdb-provider", "tmdb-main", "--ptgen-provider", "ptgen-main",
		"--accept-rule", "U2=" + sourceFingerprint, "--accept-rule", "MTEAM=" + targetFingerprint,
		"--obligation", "MTEAM:manual-upload=reviewed-by-user", "--confirm-upload",
	}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"status": "queued"`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestRetorrentConfirmationRequiresBothRuleAcceptances(t *testing.T) {
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"retorrent", "create", "--source-url", "https://u2.dmhy.org/details.php?id=1", "--target", "MTEAM",
		"--accept-rule", "MTEAM=" + strings.Repeat("b", 64), "--confirm-upload",
	}, testStreams(&output, nil))
	if !errors.Is(err, ErrReported) || !strings.Contains(output.String(), "both the source and target") {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestResumeSerializesRuleEvidenceAndConfirmation(t *testing.T) {
	jobID := "00000000-0000-0000-0000-000000000001"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v2/jobs/"+jobID+"/resume" {
			t.Fatalf("path = %s", request.URL.Path)
		}
		body, _ := io.ReadAll(request.Body)
		if !bytes.Contains(body, []byte(`"confirm_upload":true`)) || !bytes.Contains(body, []byte(`"accepted":true`)) || !bytes.Contains(body, []byte(`"evidence":"checked"`)) {
			t.Fatalf("body = %s", body)
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"queued"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "jobs", "resume", jobID,
		"--accept-rule", "U2=" + strings.Repeat("a", 64),
		"--accept-rule", "MTEAM=" + strings.Repeat("c", 64),
		"--obligation", "MTEAM:manual-upload=checked", "--confirm-upload",
	}, testStreams(&output, nil))
	if err != nil {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestShellUsesSameCommandParserAndContinues(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/health/ready" {
			t.Fatalf("path = %s", request.URL.Path)
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"ready"}`))
	}))
	defer server.Close()
	var output, diagnostic bytes.Buffer
	streams := testStreams(&output, nil)
	streams.In = strings.NewReader("health\nunknown 'unterminated\nexit\n")
	streams.Err = &diagnostic
	err := Run(context.Background(), []string{"--api-url", server.URL, "shell"}, streams)
	if err != nil || !strings.Contains(output.String(), `"status": "ready"`) || !strings.Contains(output.String(), "unfinished quote") || !strings.Contains(diagnostic.String(), "ua> ") {
		t.Fatalf("Run() err=%v output=%s diagnostic=%s", err, output.String(), diagnostic.String())
	}
}

func TestAPIFailureIsReturnedAsStableJSON(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusUnauthorized)
		_, _ = response.Write([]byte(`{"ok":false,"status":"failed","error":{"code":"invalid_token","detail":"authentication failed"},"blockers":[{"code":"invalid_token","message":"authentication failed"}]}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{"--api-url", server.URL, "tools"}, testStreams(&output, nil))
	if !errors.Is(err, ErrReported) || !strings.Contains(output.String(), `"code": "invalid_token"`) || !strings.Contains(output.String(), `"ok": false`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestProviderProbeStageAndRuleRevisionAnalysisAreExplicit(t *testing.T) {
	providerID := "22222222-2222-4222-8222-222222222222"
	revisionID := "33333333-3333-4333-8333-333333333333"
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requests++
		switch requests {
		case 1:
			if request.Method != http.MethodPost || request.URL.Path != "/api/v2/llm-providers/"+providerID+"/probe" || request.URL.Query().Get("stage") != "inference" {
				t.Fatalf("provider probe request = %s %s query=%v", request.Method, request.URL.Path, request.URL.Query())
			}
		case 2:
			if request.Method != http.MethodPost || request.URL.Path != "/api/v2/site-rules/analyze" {
				t.Fatalf("rule analysis request = %s %s", request.Method, request.URL.Path)
			}
			var body map[string]any
			if err := json.NewDecoder(request.Body).Decode(&body); err != nil || body["provider_id"] != providerID || body["source_revision_id"] != revisionID {
				t.Fatalf("rule analysis body = %#v err=%v", body, err)
			}
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"ready"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	if err := Run(context.Background(), []string{"--api-url", server.URL, "providers", "probe", providerID, "--stage", "inference"}, testStreams(&output, nil)); err != nil {
		t.Fatal(err)
	}
	if err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "analyze", revisionID, "--provider", providerID, "--confirm"}, testStreams(&output, nil)); err != nil {
		t.Fatal(err)
	}
	if requests != 2 {
		t.Fatalf("requests = %d, want 2", requests)
	}
}

func TestAuditListUsesExactFiltersAndStableCursor(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		query := request.URL.Query()
		if request.Method != http.MethodGet || request.URL.Path != "/api/v2/audit-events" ||
			query.Get("actor_type") != "worker" || query.Get("action") != "downloader.torrent.add" ||
			query.Get("resource_type") != "downloader" || query.Get("resource_id") != "box" ||
			query.Get("limit") != "20" || query.Get("cursor") != "opaque" {
			t.Fatalf("request = %s %s query=%v", request.Method, request.URL.Path, query)
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"ready","audit_events":[],"has_more":false,"next_cursor":"","blockers":[],"next_actions":[]}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "audit", "list", "--actor-type", "worker",
		"--action", "downloader.torrent.add", "--resource-type", "downloader",
		"--resource-id", "box", "--limit", "20", "--cursor", "opaque",
	}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"audit_events": []`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestNotificationReconciliationSendsExplicitEvidence(t *testing.T) {
	id := "77777777-7777-4777-8777-777777777777"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/api/v2/notifications/"+id+"/reconcile" {
			t.Fatalf("request = %s %s", request.Method, request.URL.Path)
		}
		body, _ := io.ReadAll(request.Body)
		var payload map[string]any
		if json.Unmarshal(body, &payload) != nil || payload["decision"] != "verified_delivered" || payload["confirmed"] != true || payload["message_id"] != "1234567890" || payload["evidence_sha256"] != strings.Repeat("a", 64) {
			t.Fatalf("reconciliation payload = %s", body)
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"sent","notification_id":"` + id + `"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "notifications", "reconcile", id,
		"--decision", "verified_delivered", "--evidence-sha256", strings.Repeat("a", 64),
		"--observed-at", "2026-08-08T12:00:00Z", "--message-id", "1234567890", "--confirm",
	}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"status": "sent"`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestJobAttemptsUsesOpaquePaginationCursor(t *testing.T) {
	jobID := "00000000-0000-0000-0000-000000000001"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet || request.URL.Path != "/api/v2/jobs/"+jobID+"/attempts" ||
			request.URL.Query().Get("limit") != "25" || request.URL.Query().Get("cursor") != "opaque-attempt-cursor" {
			t.Fatalf("request = %s %s query=%v", request.Method, request.URL.Path, request.URL.Query())
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"blocked","job_id":"` + jobID + `","attempts":[],"has_more":false,"next_cursor":"","blockers":[],"next_actions":[]}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "jobs", "attempts", jobID, "--limit", "25", "--cursor", "opaque-attempt-cursor",
	}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"attempts": []`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestJobReplayUsesFreshStepModeAndIdempotencyKey(t *testing.T) {
	jobID := "00000000-0000-0000-0000-000000000001"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/api/v2/jobs/"+jobID+"/replay" ||
			request.Header.Get("Idempotency-Key") != "replay-intent" {
			t.Fatalf("request = %s %s key=%q", request.Method, request.URL.Path, request.Header.Get("Idempotency-Key"))
		}
		body, _ := io.ReadAll(request.Body)
		if string(body) != `{"execution_mode":"step","stop_after_step":"target_duplicate_check"}` {
			t.Fatalf("replay body = %s", body)
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"queued","job_id":"00000000-0000-0000-0000-000000000002","replay_of_job_id":"` + jobID + `"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "jobs", "replay", jobID, "--stop-after-step", "target_duplicate_check", "--idempotency-key", "replay-intent",
	}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"replay_of_job_id": "`+jobID+`"`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestAdaptersUsesOptionalKindFilter(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet || request.URL.Path != "/api/v2/adapters" || request.URL.Query().Get("kind") != "site" {
			t.Fatalf("request = %s %s", request.Method, request.URL.String())
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"ready","catalog_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","adapters":[]}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{"--api-url", server.URL, "adapters", "--kind", "site"}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"catalog_sha256": "aaaaaaaa`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestRulesImportReadsLocalMarkdownAndCreatesDraft(t *testing.T) {
	rulePath := filepath.Join(t.TempDir(), "U2.md")
	ruleMarkdown := "---\nschema_version: 1\n---\n\n# 原始规则\nfixture\n"
	if err := os.WriteFile(rulePath, []byte(ruleMarkdown), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/api/v2/site-rules/import" {
			t.Fatalf("request = %s %s", request.Method, request.URL.Path)
		}
		var payload struct {
			Markdown string `json:"markdown"`
		}
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil || payload.Markdown != ruleMarkdown {
			t.Fatalf("import payload/error = %q/%v", payload.Markdown, err)
		}
		response.WriteHeader(http.StatusCreated)
		_, _ = response.Write([]byte(`{"ok":true,"status":"draft","rule_revision_id":"11111111-1111-4111-8111-111111111111"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "import", "--file", rulePath}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"status": "draft"`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestRulesImportAcceptsBoundedStdin(t *testing.T) {
	ruleMarkdown := "---\nschema_version: 1\n---\n\n# 原始规则\nstdin fixture\n"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		var payload struct {
			Markdown string `json:"markdown"`
		}
		if request.Method != http.MethodPost || request.URL.Path != "/api/v2/site-rules/import" || json.NewDecoder(request.Body).Decode(&payload) != nil || payload.Markdown != ruleMarkdown {
			t.Fatalf("request = %s %s markdown=%q", request.Method, request.URL.Path, payload.Markdown)
		}
		response.WriteHeader(http.StatusCreated)
		_, _ = response.Write([]byte(`{"ok":true,"status":"draft"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "import", "--file", "-"}, testStreams(&output, strings.NewReader(ruleMarkdown)))
	if err != nil || !strings.Contains(output.String(), `"status": "draft"`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func TestRulesConfigureSourcesAndCollectRequireExplicitEvidence(t *testing.T) {
	providerID := "22222222-2222-4222-8222-222222222222"
	runID := "33333333-3333-4333-8333-333333333333"
	fingerprint := strings.Repeat("c", 64)
	sourceJSON := `{"sources":[{"id":"titles","url":"https://wiki.example.invalid/title-rules","scope":"标题规则","auth_mode":"none"}],"scope_confirmed":true,"cookie_hosts_confirmed":false}`
	requestNumber := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestNumber++
		switch requestNumber {
		case 1:
			if request.Method != http.MethodPut || request.URL.Path != "/api/v2/sites/MTEAM/rule-sources" {
				t.Fatalf("source request = %s %s", request.Method, request.URL.Path)
			}
			var body map[string]any
			if json.NewDecoder(request.Body).Decode(&body) != nil || body["scope_confirmed"] != true || body["cookie_hosts_confirmed"] != false {
				t.Fatalf("source body = %#v", body)
			}
		case 2:
			if request.Method != http.MethodPost || request.URL.Path != "/api/v2/sites/MTEAM/rule-collection-runs" || request.Header.Get("Idempotency-Key") != "collection-key" {
				t.Fatalf("collection request = %s %s headers=%v", request.Method, request.URL.Path, request.Header)
			}
			var body map[string]any
			if json.NewDecoder(request.Body).Decode(&body) != nil || body["source_set_fingerprint"] != fingerprint || body["provider_id"] != providerID || body["confirm"] != true {
				t.Fatalf("collection body = %#v", body)
			}
		case 3:
			if request.Method != http.MethodGet || request.URL.Path != "/api/v2/site-rule-collection-runs/"+runID {
				t.Fatalf("status request = %s %s", request.Method, request.URL.Path)
			}
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"ready"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	if err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "sources-set", "mteam", "--file", "-", "--confirm"}, testStreams(&output, strings.NewReader(sourceJSON))); err != nil {
		t.Fatal(err)
	}
	if err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "collect", "mteam", "--fingerprint", fingerprint, "--provider", providerID, "--idempotency-key", "collection-key", "--confirm"}, testStreams(&output, nil)); err != nil {
		t.Fatal(err)
	}
	if err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "collection", runID}, testStreams(&output, nil)); err != nil {
		t.Fatal(err)
	}
	if requestNumber != 3 {
		t.Fatalf("requests = %d", requestNumber)
	}
}

func TestRulesApproveActivateAndDiscardRequireExplicitConfirmation(t *testing.T) {
	revisionID := "11111111-1111-4111-8111-111111111111"
	fingerprint := strings.Repeat("a", 64)
	requestNumber := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestNumber++
		expectedPath := "/api/v2/site-rules/" + revisionID + "/approve"
		if requestNumber == 2 {
			expectedPath = "/api/v2/site-rules/" + revisionID + "/activate"
		} else if requestNumber == 3 {
			expectedPath = "/api/v2/site-rules/" + revisionID + "/discard"
		}
		if request.Method != http.MethodPost || request.URL.Path != expectedPath {
			t.Fatalf("request %d = %s %s", requestNumber, request.Method, request.URL.Path)
		}
		body, _ := io.ReadAll(request.Body)
		if requestNumber == 1 && (!bytes.Contains(body, []byte(`"fingerprint":"`+fingerprint+`"`)) || !bytes.Contains(body, []byte(`"comment":"reviewed current rules"`))) {
			t.Fatalf("approval body = %s", body)
		}
		if requestNumber == 2 && string(body) != `{}` {
			t.Fatalf("activation body = %s", body)
		}
		if requestNumber == 3 && (!bytes.Contains(body, []byte(`"fingerprint":"`+fingerprint+`"`)) || !bytes.Contains(body, []byte(`"confirm":true`))) {
			t.Fatalf("discard body = %s", body)
		}
		_, _ = response.Write([]byte(`{"ok":true,"status":"approved"}`))
	}))
	defer server.Close()

	for _, command := range [][]string{
		{"rules", "approve", revisionID, "--fingerprint", fingerprint},
		{"rules", "activate", revisionID},
		{"rules", "discard", revisionID, "--fingerprint", fingerprint},
	} {
		var output bytes.Buffer
		err := Run(context.Background(), command, testStreams(&output, nil))
		if !errors.Is(err, ErrReported) || !strings.Contains(output.String(), "--confirm") {
			t.Fatalf("Run(%v) err=%v output=%s", command, err, output.String())
		}
	}

	var output bytes.Buffer
	err := Run(context.Background(), []string{"--api-url", server.URL, "rules", "approve", revisionID, "--fingerprint", fingerprint, "--comment", "reviewed current rules", "--confirm"}, testStreams(&output, nil))
	if err != nil {
		t.Fatalf("approve err=%v output=%s", err, output.String())
	}
	output.Reset()
	err = Run(context.Background(), []string{"--api-url", server.URL, "rules", "activate", revisionID, "--confirm"}, testStreams(&output, nil))
	if err != nil || requestNumber != 2 {
		t.Fatalf("activate err=%v requests=%d output=%s", err, requestNumber, output.String())
	}
	output.Reset()
	err = Run(context.Background(), []string{"--api-url", server.URL, "rules", "discard", revisionID, "--fingerprint", fingerprint, "--confirm"}, testStreams(&output, nil))
	if err != nil || requestNumber != 3 {
		t.Fatalf("discard err=%v requests=%d output=%s", err, requestNumber, output.String())
	}
}

func TestRulesReadCommandsValidateIdentifiersAndRoutes(t *testing.T) {
	revisionID := "11111111-1111-4111-8111-111111111111"
	wantPaths := []string{"/api/v2/sites/U2/rules", "/api/v2/sites/U2/rules/active", "/api/v2/site-rules/" + revisionID}
	requestNumber := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet || requestNumber >= len(wantPaths) || request.URL.Path != wantPaths[requestNumber] {
			t.Fatalf("request %d = %s %s", requestNumber, request.Method, request.URL.Path)
		}
		requestNumber++
		_, _ = response.Write([]byte(`{"ok":true,"status":"ready"}`))
	}))
	defer server.Close()
	for _, command := range [][]string{{"rules", "list", "u2"}, {"rules", "active", "u2"}, {"rules", "get", revisionID}} {
		var output bytes.Buffer
		if err := Run(context.Background(), append([]string{"--api-url", server.URL}, command...), testStreams(&output, nil)); err != nil {
			t.Fatalf("Run(%v) err=%v output=%s", command, err, output.String())
		}
	}
	if requestNumber != len(wantPaths) {
		t.Fatalf("requests = %d, want %d", requestNumber, len(wantPaths))
	}
}

func TestLiveReadinessUsesExactLocalOnlyInputs(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		query := request.URL.Query()
		if request.Method != http.MethodGet || request.URL.Path != "/api/v2/readiness/live" ||
			query.Get("source") != "U2" || query.Get("target") != "MTEAM" ||
			query.Get("downloader") != "box" || query.Get("target_downloader") != "seedbox" ||
			query.Get("image_host") != "imgbb" || query.Get("screenshot_profile") != "default" ||
			query.Get("tmdb_provider") != "tmdb-main" || query.Get("ptgen_provider") != "ptgen-main" {
			t.Fatalf("request = %s %s query=%v", request.Method, request.URL.Path, query)
		}
		_, _ = response.Write([]byte(`{"ok":false,"status":"blocked","configuration_ready":false,"external_calls_performed":false,"live_upload_authorized":false,"source":"U2","target":"MTEAM","checks":[],"required_confirmations":[],"blockers":[{"code":"site_configuration_required","message":"missing","component":"site.U2"}],"next_actions":[],"resume_state":{"accept_rules":{},"confirm_upload":false},"summary":"local only"}`))
	}))
	defer server.Close()
	var output bytes.Buffer
	err := Run(context.Background(), []string{
		"--api-url", server.URL, "readiness", "live", "--source", "u2", "--target", "mteam",
		"--downloader", "box", "--target-downloader", "seedbox", "--image-host", "imgbb", "--screenshot-profile", "default",
		"--tmdb-provider", "tmdb-main", "--ptgen-provider", "ptgen-main",
	}, testStreams(&output, nil))
	if err != nil || !strings.Contains(output.String(), `"external_calls_performed": false`) ||
		!strings.Contains(output.String(), `"live_upload_authorized": false`) || !strings.Contains(output.String(), `"confirm_upload": false`) {
		t.Fatalf("Run() err=%v output=%s", err, output.String())
	}
}

func testStreams(output io.Writer, input io.Reader) Streams {
	if input == nil {
		input = strings.NewReader("")
	}
	return Streams{
		In: input, Out: output, Err: io.Discard,
		Getenv: func(name string) string {
			if name == "UA_API_TOKEN" {
				return fixtureToken
			}
			return ""
		},
	}
}
