package clientcli

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
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
