package server

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestRedactJSONRemovesSecretFieldsAndURLCredentials(t *testing.T) {
	input := json.RawMessage(`{
		"source_url":"https://u2.dmhy.org/download.php?id=7&passkey=source-secret",
		"cookie":"uid=1; pass=secret-cookie",
		"announce":"https://tracker.example/announce/announce-secret",
		"token_response_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	}`)
	redacted := redactJSON(input)
	for _, secret := range []string{"source-secret", "secret-cookie", "announce-secret"} {
		if bytes.Contains(redacted, []byte(secret)) {
			t.Fatalf("redacted JSON exposed %q: %s", secret, redacted)
		}
	}
	if !bytes.Contains(redacted, []byte("REDACTED")) || !bytes.Contains(redacted, []byte(strings.Repeat("a", 64))) {
		t.Fatalf("redacted JSON lost marker or safe digest: %s", redacted)
	}
}

func TestRedactStepsReplacesInputSnapshotWithDigest(t *testing.T) {
	steps := redactSteps([]workflow.Step{{
		InputSnapshot: json.RawMessage(`{"passkey":"step-secret"}`),
		OutputSummary: json.RawMessage(`{"details_url":"https://example.invalid/details?id=1&token=output-secret"}`),
		Blockers:      json.RawMessage(`[]`), NextActions: json.RawMessage(`[]`), ResumeState: json.RawMessage(`{}`),
	}})
	if len(steps) != 1 || bytes.Contains(steps[0].InputSnapshot, []byte("step-secret")) ||
		bytes.Contains(steps[0].OutputSummary, []byte("output-secret")) {
		t.Fatalf("redacted step exposed input: %#v", steps)
	}
	var snapshot struct {
		Redacted bool   `json:"redacted"`
		SHA256   string `json:"sha256"`
	}
	if err := json.Unmarshal(steps[0].InputSnapshot, &snapshot); err != nil || !snapshot.Redacted || len(snapshot.SHA256) != 64 {
		t.Fatalf("snapshot marker/error = %#v/%v", snapshot, err)
	}
}

func TestRedactAttemptsHidesRawSnapshotAndNestedSecrets(t *testing.T) {
	attempts := redactAttempts([]workflow.Attempt{{
		InputSnapshot: json.RawMessage(`{"cookie":"attempt-input-secret"}`),
		OutputSummary: json.RawMessage(`{"nested":{"api_key":"attempt-output-secret"}}`),
		ErrorDetails:  json.RawMessage(`[{"message":"https://tracker.invalid/announce/attempt-error-secret"}]`),
	}})
	if len(attempts) != 1 {
		t.Fatalf("attempt count = %d", len(attempts))
	}
	encoded, err := json.Marshal(attempts[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"attempt-input-secret", "attempt-output-secret", "attempt-error-secret"} {
		if bytes.Contains(encoded, []byte(secret)) {
			t.Fatalf("redacted attempt exposed %q: %s", secret, encoded)
		}
	}
	var snapshot struct {
		Redacted bool   `json:"redacted"`
		SHA256   string `json:"sha256"`
	}
	if err := json.Unmarshal(attempts[0].InputSnapshot, &snapshot); err != nil || !snapshot.Redacted || len(snapshot.SHA256) != 64 {
		t.Fatalf("attempt snapshot/error = %#v/%v", snapshot, err)
	}
}
