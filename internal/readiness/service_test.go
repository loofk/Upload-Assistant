package readiness

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/rules"
)

type fakeRules struct {
	revisions map[string]rules.Revision
}

func (provider fakeRules) Active(_ context.Context, site string) (rules.Revision, error) {
	revision, exists := provider.revisions[site]
	if !exists {
		return rules.Revision{}, rules.ErrNotFound
	}
	return revision, nil
}

type fakeIntegrations struct {
	sites       map[string]integrations.RuntimeSite
	downloaders map[string]integrations.RuntimeDownloader
	imageHosts  map[string]integrations.RuntimeImageHost
	screenshots map[string]integrations.RuntimeScreenshotProfile
}

func (provider fakeIntegrations) GetRuntimeSite(_ context.Context, name string) (integrations.RuntimeSite, error) {
	value, exists := provider.sites[name]
	if !exists {
		return integrations.RuntimeSite{}, integrations.ErrNotFound
	}
	return value, nil
}

func (provider fakeIntegrations) GetRuntimeDownloader(_ context.Context, name string) (integrations.RuntimeDownloader, error) {
	value, exists := provider.downloaders[name]
	if !exists {
		return integrations.RuntimeDownloader{}, integrations.ErrNotFound
	}
	return value, nil
}

func (provider fakeIntegrations) GetRuntimeImageHost(_ context.Context, name string) (integrations.RuntimeImageHost, error) {
	value, exists := provider.imageHosts[name]
	if !exists {
		return integrations.RuntimeImageHost{}, integrations.ErrNotFound
	}
	return value, nil
}

func (provider fakeIntegrations) GetRuntimeScreenshotProfile(_ context.Context, name string) (integrations.RuntimeScreenshotProfile, error) {
	value, exists := provider.screenshots[name]
	if !exists {
		return integrations.RuntimeScreenshotProfile{}, integrations.ErrNotFound
	}
	return value, nil
}

func TestCheckReportsConfigurationReadyWithoutAuthorizingLiveUpload(t *testing.T) {
	runtime := testRuntime(t)
	provider := fakeIntegrations{
		sites: map[string]integrations.RuntimeSite{
			"U2":    {Code: "U2", Adapter: "nexusphp", ConfigurationSHA256: strings.Repeat("1", 64), Credentials: map[string]string{"cookie": "secret", "passkey": "secret"}},
			"MTEAM": {Code: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("2", 64), Credentials: map[string]string{"api_key": "secret"}},
		},
		downloaders: map[string]integrations.RuntimeDownloader{
			"box": {Downloader: integrations.Downloader{Name: "box", Adapter: "qbittorrent", Enabled: true, CredentialFields: []string{"password", "username"}, AdapterCapability: integrations.DownloaderAdapterCapability{RuntimeSupported: true}}, ConfigurationSHA256: strings.Repeat("3", 64)},
		},
		imageHosts: map[string]integrations.RuntimeImageHost{
			"imgbb": {ImageHost: integrations.ImageHost{Name: "imgbb", Adapter: "imgbb", Enabled: true, HealthStatus: "unknown"}, Credentials: map[string]string{"api_key": "secret"}},
		},
		screenshots: map[string]integrations.RuntimeScreenshotProfile{
			"default": {ScreenshotProfile: integrations.ScreenshotProfile{Name: "default", Revision: 2, Enabled: true, Config: json.RawMessage(`{"count":6,"format":"png"}`)}},
		},
	}
	ruleProvider := fakeRules{revisions: map[string]rules.Revision{
		"U2":    {ID: "rule-u2", SiteCode: "U2", Status: "approved", Fingerprint: strings.Repeat("a", 64), Obligations: mustJSON([]rules.Obligation{{ID: "manual-transfer", Blocking: true}})},
		"MTEAM": {ID: "rule-mteam", SiteCode: "MTEAM", Status: "approved", Fingerprint: strings.Repeat("b", 64), Obligations: mustJSON([]rules.Obligation{})},
	}}

	report, err := NewService(ruleProvider, provider, runtime).Check(context.Background(), Input{
		Source: "u2", Target: "mteam", Downloader: "box", ImageHost: "imgbb", ScreenshotProfile: "default",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !report.OK || report.Status != "configuration_ready" || !report.ConfigurationReady || report.ExternalCallsPerformed || report.LiveUploadAuthorized || len(report.Blockers) != 0 {
		t.Fatalf("unexpected report state: %#v", report)
	}
	if len(report.RequiredConfirmations) != 2 || report.RequiredConfirmations[0].SiteCode != "U2" || !slicesContain(report.RequiredConfirmations[0].ObligationIDs, "manual-transfer") {
		t.Fatalf("required confirmations = %#v", report.RequiredConfirmations)
	}
	resumeRules := report.ResumeState["accept_rules"].(map[string]any)
	if report.ResumeState["confirm_upload"] != false || resumeRules["U2"].(map[string]any)["accepted"] != false {
		t.Fatalf("unsafe resume template = %#v", report.ResumeState)
	}
	body, _ := json.Marshal(report)
	for _, secret := range []string{"secret"} {
		if strings.Contains(string(body), secret) {
			t.Fatalf("report exposed credential data %q: %s", secret, body)
		}
	}
}

func TestCheckReturnsActionableBlockersWithoutExternalCalls(t *testing.T) {
	report, err := NewService(fakeRules{}, fakeIntegrations{}, Runtime{
		MediaInfoBinary: "/missing/mediainfo", BDInfoBinary: "/missing/bdinfo", FFmpegBinary: "/missing/ffmpeg",
		FFprobeBinary: "/missing/ffprobe", MkbrrBinary: "/missing/mkbrr", DownloadsDir: "/missing/downloads",
	}).Check(context.Background(), Input{Source: "CHD", Target: "MTEAM", Downloader: "box", TargetDownloader: "seedbox", ImageHost: "ptpimg", ScreenshotProfile: "six"})
	if err != nil {
		t.Fatal(err)
	}
	if report.OK || report.Status != "blocked" || report.ConfigurationReady || report.ExternalCallsPerformed || report.LiveUploadAuthorized || len(report.Blockers) < 10 || len(report.NextActions) == 0 {
		t.Fatalf("unexpected blocked report: %#v", report)
	}
	if !hasBlocker(report.Blockers, "active_rule_required") || !hasBlocker(report.Blockers, "runtime_binary_required") || !hasBlocker(report.Blockers, "downloads_mount_required") {
		t.Fatalf("missing expected blockers: %#v", report.Blockers)
	}
}

func TestCheckRejectsUnsupportedReferenceFlowAndUnsafeNames(t *testing.T) {
	service := NewService(fakeRules{}, fakeIntegrations{}, Runtime{})
	for _, input := range []Input{
		{Source: "TTG", Target: "MTEAM", Downloader: "box", ImageHost: "imgbb", ScreenshotProfile: "default"},
		{Source: "U2", Target: "CHD", Downloader: "box", ImageHost: "imgbb", ScreenshotProfile: "default"},
		{Source: "U2", Target: "MTEAM", Downloader: "bad/name", ImageHost: "imgbb", ScreenshotProfile: "default"},
	} {
		if _, err := service.Check(context.Background(), input); !errors.Is(err, ErrInvalid) {
			t.Fatalf("input %#v error = %v", input, err)
		}
	}
}

func testRuntime(t *testing.T) Runtime {
	t.Helper()
	directory := t.TempDir()
	binaries := make([]string, 5)
	for index, name := range []string{"mediainfo", "bdinfo", "ffmpeg", "ffprobe", "mkbrr"} {
		path := filepath.Join(directory, name)
		if err := os.WriteFile(path, []byte("fixture"), 0o700); err != nil {
			t.Fatal(err)
		}
		binaries[index] = path
	}
	downloads := filepath.Join(directory, "downloads")
	if err := os.Mkdir(downloads, 0o750); err != nil {
		t.Fatal(err)
	}
	return Runtime{MediaInfoBinary: binaries[0], BDInfoBinary: binaries[1], FFmpegBinary: binaries[2], FFprobeBinary: binaries[3], MkbrrBinary: binaries[4], DownloadsDir: downloads}
}

func mustJSON(value any) json.RawMessage {
	body, _ := json.Marshal(value)
	return body
}

func hasBlocker(blockers []Blocker, code string) bool {
	for _, blocker := range blockers {
		if blocker.Code == code {
			return true
		}
	}
	return false
}

func slicesContain(values []string, value string) bool {
	for _, item := range values {
		if item == value {
			return true
		}
	}
	return false
}
