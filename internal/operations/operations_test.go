package operations

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/rules"
)

func TestRedactRecursivelyRemovesSecretsAndURLs(t *testing.T) {
	value := map[string]any{"nested": []any{map[string]any{"api_key": "secret", "url": "https://user:pass@example.invalid/announce/passkey?token=abc&ok=1"}}, "password_sha256": "evidence", "prompt_tokens": 42, "max_completion_tokens": 4096, "completion_tokens_details": map[string]any{"reasoning_tokens": 2048}}
	redacted := Redact(value).(map[string]any)
	body, _ := json.Marshal(redacted)
	text := string(body)
	for _, secret := range []string{"secret", "user:pass", "passkey?", "token=abc"} {
		if strings.Contains(text, secret) {
			t.Fatalf("redacted body contains %q: %s", secret, text)
		}
	}
	if !strings.Contains(text, "password_sha256") || !strings.Contains(text, "evidence") {
		t.Fatalf("digest evidence was removed: %s", text)
	}
	if !strings.Contains(text, `"prompt_tokens":42`) || !strings.Contains(text, `"max_completion_tokens":4096`) || !strings.Contains(text, `"reasoning_tokens":2048`) {
		t.Fatalf("non-secret token counts were removed: %s", text)
	}
}

func TestValidateProviderURLTrustLevels(t *testing.T) {
	for _, test := range []struct {
		raw, level string
		valid      bool
	}{{"http://ollama:11434/v1", "local", true}, {"http://127.0.0.1:11434/v1", "local", true}, {"https://models.example.invalid/v1", "remote", true}, {"http://models.example.invalid/v1", "remote", false}, {"http://169.254.169.254/latest", "local", false}, {"https://user:pass@example.invalid/v1", "remote", false}, {"http://public.example.invalid/v1", "local", false}} {
		_, err := ValidateProviderURL(test.raw, test.level)
		if (err == nil) != test.valid {
			t.Errorf("ValidateProviderURL(%q,%q) error=%v valid=%t", test.raw, test.level, err, test.valid)
		}
	}
}

func TestValidateDiagnosticResultRejectsFabricatedEvidence(t *testing.T) {
	valid := DiagnosticResult{Summary: "bounded", Severity: "warning", Confidence: .7, PossibleCauses: []string{"cause"}, EvidenceRefs: []string{"log:1"}, Recommendations: []string{"inspect"}, Risks: []string{}, Limitations: []string{}}
	if err := validateDiagnosticResult(valid, []string{"log:1"}); err != nil {
		t.Fatal(err)
	}
	valid.EvidenceRefs = []string{"log:2"}
	if err := validateDiagnosticResult(valid, []string{"log:1"}); err == nil {
		t.Fatal("fabricated evidence ref accepted")
	}
}

func TestRuleAnalysisDraftIsConservativeAndPreservesOriginalText(t *testing.T) {
	input := RuleAnalysisInput{
		SiteCode: "CHD", DisplayName: "彩虹岛", Roles: []string{"source"},
		SourceURL: "https://rules.example.invalid/faq", SourceScope: "规则与常见问题全文",
	}
	extraction := ruleExtraction{
		Automation:  rules.Automation{Download: true, AutoPull: true, AutoUpload: true},
		Obligations: []ruleExtractionObligation{{Description: "人工确认转载范围", Scope: "retorrent", Enforcement: providerText{Value: "review"}}},
		Confidence:  .7,
	}
	original := "第一条：禁止重复发布。\n第二条：做种时间以站内页面为准。"
	markdown, digest, warnings, err := buildRuleAnalysisDraft(input, original, extraction)
	if err != nil {
		t.Fatal(err)
	}
	document, err := rules.ParseMarkdown([]byte(markdown))
	if err != nil {
		t.Fatal(err)
	}
	if document.Body != original || document.Source.TextSHA256 != digest {
		t.Fatalf("original text/hash were not preserved: body=%q hash=%q", document.Body, document.Source.TextSHA256)
	}
	if document.Automation.AutoPull || document.Automation.AutoUpload || !document.Automation.ManualReviewRequired {
		t.Fatalf("unsafe automation flags survived: %#v", document.Automation)
	}
	if document.Access.ServiceAccess != "undetermined" || document.Access.SearchAccess != "undetermined" {
		t.Fatalf("missing access evidence was not fail-closed: %#v", document.Access)
	}
	if len(document.Obligations) != 0 || len(document.Advisories) != 1 || document.Advisories[0].Summary != "人工确认转载范围" {
		t.Fatalf("AI guidance was not reduced to a preflight advisory: obligations=%#v advisories=%#v", document.Obligations, document.Advisories)
	}
	if len(warnings) < 2 {
		t.Fatalf("expected access and incomplete-source warnings: %#v", warnings)
	}
}

func TestReasoningEffortIsOnlySentWhenExplicit(t *testing.T) {
	request := map[string]any{}
	applyReasoningEffort(request, Provider{ReasoningEffort: "default"})
	if _, exists := request["reasoning_effort"]; exists {
		t.Fatal("default reasoning effort should be omitted for provider compatibility")
	}
	applyReasoningEffort(request, Provider{ReasoningEffort: "high"})
	if request["reasoning_effort"] != "high" {
		t.Fatalf("reasoning effort = %#v", request["reasoning_effort"])
	}
}

type blockingWriter struct {
	gate   chan struct{}
	writes int
}

func (w *blockingWriter) InsertLog(context.Context, LogEntry) (int64, error) {
	w.writes++
	<-w.gate
	return int64(w.writes), nil
}
func TestAsyncLogSinkDropsWhenBoundedQueueIsFull(t *testing.T) {
	writer := &blockingWriter{gate: make(chan struct{})}
	sink := NewAsyncLogSink(writer, nil, 1)
	sink.Enqueue(LogEntry{Level: "info", Component: "test", Message: "first"})
	sink.Enqueue(LogEntry{Level: "info", Component: "test", Message: "second"})
	if sink.Dropped() != 1 {
		t.Fatalf("dropped=%d want 1", sink.Dropped())
	}
	close(writer.gate)
}

type failingWriter struct{}

func (failingWriter) InsertLog(context.Context, LogEntry) (int64, error) {
	return 0, errors.New("database unavailable")
}

func TestAsyncLogSinkCountsDatabaseFailuresWithoutBlockingProducer(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	sink := NewAsyncLogSink(failingWriter{}, nil, 2)
	go sink.Run(ctx)
	sink.Enqueue(LogEntry{Level: "info", Component: "test", Message: "database failure"})
	deadline := time.Now().Add(time.Second)
	for sink.Dropped() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	cancel()
	sink.Wait()
	if sink.Dropped() != 1 {
		t.Fatalf("database failure count=%d want 1", sink.Dropped())
	}
}

func TestProviderClientDoesNotFollowRedirects(t *testing.T) {
	var targetCalled atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { targetCalled.Store(true) }))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, http.StatusFound) }))
	defer redirect.Close()
	client, err := (&DiagnosticService{}).clientFor(Provider{BaseURL: redirect.URL, DataLevel: "local", TimeoutSeconds: 2})
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.Get(redirect.URL)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusFound || targetCalled.Load() {
		t.Fatalf("redirect status/target = %d/%t", response.StatusCode, targetCalled.Load())
	}
}

func TestGenerateAgeIdentityWhenToolAvailable(t *testing.T) {
	if _, err := exec.LookPath("age-keygen"); err != nil {
		t.Skip("age-keygen is not installed on host")
	}
	manager := &BackupManager{}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	identity, recipient, err := manager.GenerateIdentity(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(identity, "AGE-SECRET-KEY-") || !strings.HasPrefix(recipient, "age1") {
		t.Fatalf("invalid identity/recipient")
	}
}

func TestParseAgeIdentityAcrossAgeKeygenFormats(t *testing.T) {
	identity := "AGE-SECRET-KEY-1LOCALFIXTURE"
	for _, fixture := range []struct {
		stdout string
		stderr string
	}{
		{stdout: identity + "\n", stderr: "Public key: age1localfixture\n"},
		{stdout: "# created: now\n# public key: age1localfixture\n" + identity + "\n", stderr: "age-keygen: warning\n"},
	} {
		gotIdentity, recipient, err := parseAgeIdentity(fixture.stdout, fixture.stderr)
		if err != nil || gotIdentity != identity || recipient != "age1localfixture" {
			t.Fatalf("parseAgeIdentity() = %q, %q, %v", gotIdentity, recipient, err)
		}
	}
}

func TestDatabaseEnvironmentUsesDiscreteLibpqVariables(t *testing.T) {
	t.Setenv("PGDATABASE", "wrong-database")
	environment, err := databaseEnvironment("postgres://backup-user:fixture-password@db.invalid:5544/source?sslmode=disable", "restored")
	if err != nil {
		t.Fatal(err)
	}
	values := map[string]string{}
	databaseEntries := 0
	for _, item := range environment {
		name, value, _ := strings.Cut(item, "=")
		if strings.HasPrefix(name, "PG") {
			values[name] = value
		}
		if name == "PGDATABASE" {
			databaseEntries++
		}
	}
	if databaseEntries != 1 || values["PGHOST"] != "db.invalid" || values["PGPORT"] != "5544" || values["PGUSER"] != "backup-user" || values["PGPASSWORD"] != "fixture-password" || values["PGDATABASE"] != "restored" || values["PGSSLMODE"] != "disable" {
		t.Fatalf("unexpected libpq environment: %#v (database entries %d)", values, databaseEntries)
	}
}

func TestExtractTarRejectsMissingArchive(t *testing.T) {
	if err := extractTar(t.TempDir()+"/missing.tar", t.TempDir()); err == nil {
		t.Fatal("missing archive accepted")
	}
}

func TestCapacityThresholdsAndHysteresis(t *testing.T) {
	tests := []struct {
		previous string
		value    float64
		want     string
	}{
		{"normal", 79.9, "normal"}, {"normal", 80, "warning"}, {"normal", 90, "critical"},
		{"critical", 85, "critical"}, {"critical", 84.9, "warning"},
		{"warning", 75, "warning"}, {"warning", 74.9, "normal"},
	}
	for _, test := range tests {
		if got := capacityStatus(test.previous, test.value, 80, 90, 5); got != test.want {
			t.Errorf("capacityStatus(%s, %.1f) = %s, want %s", test.previous, test.value, got, test.want)
		}
	}
}

func TestDailyBackupScheduleValidation(t *testing.T) {
	if hour, minute, err := parseDailySchedule("30 3 * * *"); err != nil || hour != 3 || minute != 30 {
		t.Fatalf("default schedule = %d:%d, %v", hour, minute, err)
	}
	for _, invalid := range []string{"* * * * *", "60 3 * * *", "30 25 * * *", "30 3 * * 1"} {
		if _, _, err := parseDailySchedule(invalid); err == nil {
			t.Errorf("schedule %q was accepted", invalid)
		}
	}
}
