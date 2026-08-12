package legacy

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestInspectBuildsRedactedMigrationPlan(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {
    "default_torrent_client": "box",
    "img_host_1": "imgbb",
    "imgbb_api": "imgbb-private-value",
    "screens": "5",
    "tone_map": True,
  },
  "TRACKERS": {
    "U2": {"passkey": "u2-private-passkey", "qbit_upload_limit": 100},
    "MTEAM": {"api_key": "mteam-private-key"},
  },
  "TORRENT_CLIENTS": {
    "box": {
      "torrent_client": "qbit",
      "qbit_url": "https://qb.example.test",
      "qbit_port": "443",
      "qbit_user": "operator",
      "qbit_pass": "qbit-private-password",
      "local_path": ["/downloads"],
      "remote_path": ["/srv/downloads"],
    },
  },
}`)
	if err := os.Mkdir(filepath.Join(root, "cookies"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "cookies", "U2.txt"), []byte("session=u2-private-cookie\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if !plan.OK || plan.Status != "ready" || len(plan.SourceFingerprint) != 64 || len(plan.files) != 2 {
		t.Fatalf("unexpected preview readiness: ok=%v status=%s fingerprint=%q files=%d", plan.OK, plan.Status, plan.SourceFingerprint, len(plan.files))
	}
	if len(plan.sites) != 3 || len(plan.downloaders) != 1 || len(plan.imageHosts) != 1 || len(plan.screenshots) != 1 {
		t.Fatalf("unexpected private operation counts: sites=%d downloaders=%d image_hosts=%d screenshots=%d", len(plan.sites), len(plan.downloaders), len(plan.imageHosts), len(plan.screenshots))
	}
	if plan.downloaders[0].input.Enabled == nil || !*plan.downloaders[0].input.Enabled {
		t.Fatal("remote default qBittorrent should be enabled")
	}
	if len(plan.downloaders[0].input.PathMappings) != 1 || plan.downloaders[0].input.PathMappings[0].RemotePath != "/srv/downloads" {
		t.Fatalf("unexpected path mappings: %#v", plan.downloaders[0].input.PathMappings)
	}
	body, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"imgbb-private-value", "u2-private-passkey", "mteam-private-key", "qbit-private-password", "u2-private-cookie"} {
		if strings.Contains(string(body), secret) {
			t.Fatalf("serialized plan exposed a secret for resource %q", secret[:4])
		}
	}
	if !strings.Contains(string(body), `"credential_fields"`) || !strings.Contains(string(body), `"source_fingerprint"`) {
		t.Fatalf("serialized plan lacks redacted decision fields: %s", body)
	}
	if !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool { return issue.Code == "site_rule_limits_require_manual_review" }) {
		t.Fatalf("warnings do not contain rule-limit review: %#v", plan.Warnings)
	}
}

func TestInspectMigratesSelectedKeylessImageHostsWithoutInventingCredentials(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"img_host_1": "imgbox", "img_host_2": "pixhost"},
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.imageHosts) != 2 || plan.imageHosts[0].name != "imgbox" || plan.imageHosts[1].name != "pixhost" {
		t.Fatalf("keyless image-host migration plan = %#v", plan.imageHosts)
	}
	for _, operation := range plan.imageHosts {
		if len(operation.input.Credentials) != 0 || operation.input.Enabled == nil || !*operation.input.Enabled {
			t.Fatalf("keyless image-host migration invented credentials: %#v", operation)
		}
	}
}

func TestInspectDisablesContainerLoopbackDownloader(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"default_torrent_client": "qbittorrent"},
  "TORRENT_CLIENTS": {"qbittorrent": {
    "torrent_client": "qbit", "qbit_url": "http://127.0.0.1", "qbit_port": "8080",
    "qbit_user": "operator", "qbit_pass": "not-a-placeholder"
  }}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.downloaders) != 1 || plan.downloaders[0].input.Enabled == nil || *plan.downloaders[0].input.Enabled {
		t.Fatalf("loopback downloader was not disabled")
	}
	if !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool { return issue.Code == "container_loopback_requires_review" }) {
		t.Fatalf("warnings = %#v", plan.Warnings)
	}
}

func TestInspectMigratesSonarrAndRadarrWithoutExposingAPIKeys(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {
    "use_sonarr": True, "sonarr_url": "https://arr.example/sonarr", "sonarr_api_key": "sonarr-private-key",
    "sonarr_url_1": "http://127.0.0.1:8990", "sonarr_api_key_1": "sonarr-loopback-key",
    "use_radarr": True, "radarr_url": "https://arr.example/radarr", "radarr_api_key": "radarr-private-key"
  },
  "DISCORD": {"use_discord": True, "discord_bot_token": "bot-private-token", "discord_channel_id": "123"}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.mediaManagers) != 3 || plan.mediaManagers[0].input.Adapter != "sonarr" || plan.mediaManagers[0].input.Enabled == nil || !*plan.mediaManagers[0].input.Enabled {
		t.Fatalf("media managers = %#v", plan.mediaManagers)
	}
	if plan.mediaManagers[1].input.Enabled == nil || *plan.mediaManagers[1].input.Enabled {
		t.Fatalf("loopback Sonarr should be preserved disabled: %#v", plan.mediaManagers[1])
	}
	if !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool { return issue.Code == "legacy_discord_bot_requires_webhook" }) ||
		!slices.ContainsFunc(plan.Warnings, func(issue Issue) bool {
			return issue.Code == "container_loopback_requires_review" && issue.Resource == "sonarr-1"
		}) {
		t.Fatalf("warnings = %#v", plan.Warnings)
	}
	body, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"sonarr-private-key", "sonarr-loopback-key", "radarr-private-key", "bot-private-token"} {
		if strings.Contains(string(body), secret) {
			t.Fatalf("preview exposed secret: %s", body)
		}
	}
}

func TestInspectMigratesTransmissionWithoutExposingCredentials(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"default_torrent_client": "transmission"},
  "TORRENT_CLIENTS": {"transmission": {
    "torrent_client": "transmission", "transmission_protocol": "https",
    "transmission_host": "seedbox.example", "transmission_port": 9091,
    "transmission_path": "/transmission/rpc", "transmission_username": "operator",
    "transmission_password": "private-password", "transmission_label": "retorrent",
    "local_path": ["/downloads"], "remote_path": ["/srv/downloads"]
  }}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.downloaders) != 1 {
		t.Fatalf("downloaders = %#v", plan.downloaders)
	}
	operation := plan.downloaders[0]
	if operation.input.Adapter != "transmission" || operation.input.Enabled == nil || !*operation.input.Enabled ||
		operation.input.Config.Endpoint != "https://seedbox.example:9091/transmission/rpc" ||
		operation.input.Credentials["username"] != "operator" || operation.input.Credentials["password"] != "private-password" ||
		len(operation.input.PathMappings) != 1 {
		t.Fatalf("Transmission operation = %#v", operation)
	}
	serialized, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(serialized), "private-password") || !strings.Contains(string(serialized), `"adapter":"transmission"`) {
		t.Fatalf("redacted Transmission preview = %s", serialized)
	}
}

func TestInspectMigratesRTorrentURLCredentialsWithoutExposingThem(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"default_torrent_client": "rtorrent"},
  "TORRENT_CLIENTS": {"rtorrent": {
    "torrent_client": "rtorrent",
    "rtorrent_url": "https://operator:private-password@seedbox.example:443/operator/rutorrent/plugins/httprpc/action.php",
    "rtorrent_label": "retorrent", "torrent_storage_dir": "/srv/rtorrent/session",
    "local_path": ["/downloads"], "remote_path": ["/srv/downloads"]
  }}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.downloaders) != 1 {
		t.Fatalf("downloaders = %#v", plan.downloaders)
	}
	operation := plan.downloaders[0]
	if operation.input.Adapter != "rtorrent" || operation.input.Enabled == nil || !*operation.input.Enabled ||
		operation.input.Config.Endpoint != "https://seedbox.example:443/operator/rutorrent/plugins/httprpc/action.php" ||
		operation.input.Credentials["username"] != "operator" || operation.input.Credentials["password"] != "private-password" ||
		operation.input.Config.Options["label"] != "retorrent" || len(operation.input.PathMappings) != 1 {
		t.Fatalf("rTorrent operation = %#v", operation)
	}
	if !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool { return issue.Code == "legacy_rtorrent_session_path_not_imported" }) {
		t.Fatalf("warnings = %#v", plan.Warnings)
	}
	serialized, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(serialized), "private-password") || strings.Contains(string(serialized), "operator:private") || !strings.Contains(string(serialized), `"adapter":"rtorrent"`) {
		t.Fatalf("redacted rTorrent preview = %s", serialized)
	}
}

func TestInspectDisablesRTorrentWithIncompleteURLCredentials(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"default_torrent_client": "rtorrent"},
  "TORRENT_CLIENTS": {"rtorrent": {
    "torrent_client": "rtorrent", "rtorrent_url": "http://operator@seedbox.example/RPC2"
  }}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.downloaders) != 1 || plan.downloaders[0].input.Enabled == nil || *plan.downloaders[0].input.Enabled || len(plan.downloaders[0].input.Credentials) != 0 {
		t.Fatalf("incomplete rTorrent migration = %#v", plan.downloaders)
	}
	if !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool { return issue.Code == "legacy_rtorrent_credentials_incomplete" }) {
		t.Fatalf("warnings = %#v", plan.Warnings)
	}
}

func TestInspectRejectsEncodedRTorrentPathTraversal(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "TORRENT_CLIENTS": {"rtorrent": {
    "torrent_client": "rtorrent", "rtorrent_url": "https://seedbox.example/%2e%2e/RPC2"
  }}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.downloaders) != 0 || !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool { return issue.Code == "legacy_rtorrent_endpoint_invalid" }) {
		t.Fatalf("encoded traversal migration = downloaders %#v warnings %#v", plan.downloaders, plan.Warnings)
	}
}

func TestInspectDefersLegacyNativeDelugeRPCWithoutExposingCredentials(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"default_torrent_client": "deluge"},
  "TORRENT_CLIENTS": {"deluge": {
    "torrent_client": "deluge", "deluge_url": "seedbox.example", "deluge_port": 58846,
    "deluge_user": "operator", "deluge_pass": "private-native-password"
  }}
}`)
	plan, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.downloaders) != 0 || !slices.ContainsFunc(plan.Warnings, func(issue Issue) bool {
		return issue.Code == "legacy_deluge_web_endpoint_required" && issue.Resource == "deluge"
	}) {
		t.Fatalf("Deluge migration = downloaders %#v warnings %#v", plan.downloaders, plan.Warnings)
	}
	serialized, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(serialized), "private-native-password") || strings.Contains(string(serialized), "operator") {
		t.Fatalf("redacted Deluge preview exposed native RPC credentials: %s", serialized)
	}
}

func TestInspectRejectsSymlinkAndExecutableConfig(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(t.TempDir(), "config.py")
	if err := os.WriteFile(target, []byte("config = {}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(root, "config.py")); err != nil {
		t.Fatal(err)
	}
	if _, err := inspectLegacyFixture(root); err == nil || !strings.Contains(err.Error(), "non-symlink") {
		t.Fatalf("symlink error = %v", err)
	}

	root = t.TempDir()
	writeLegacyFixture(t, root, "import os\nconfig = {'token': os.getenv('TOKEN')}\n")
	if _, err := inspectLegacyFixture(root); err == nil || !strings.Contains(err.Error(), "top-level config assignment") {
		t.Fatalf("executable config error = %v", err)
	}
}

func TestInspectFingerprintChangesWithCookieAndIgnoresUnknownCookieFiles(t *testing.T) {
	root := t.TempDir()
	writeLegacyFixture(t, root, `config = {"TRACKERS": {"U2": {"passkey": "real-value"}}}`)
	if err := os.Mkdir(filepath.Join(root, "cookies"), 0o750); err != nil {
		t.Fatal(err)
	}
	unknownPath := filepath.Join(root, "cookies", "UNKNOWN.txt")
	if err := os.WriteFile(unknownPath, []byte("unknown-secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	first, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(first.SourceFiles) != 1 {
		t.Fatalf("unknown cookie was inspected: %#v", first.SourceFiles)
	}
	u2Path := filepath.Join(root, "cookies", "U2.txt")
	if err := os.WriteFile(u2Path, []byte("session=one"), 0o600); err != nil {
		t.Fatal(err)
	}
	second, err := inspectLegacyFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	if first.SourceFingerprint == second.SourceFingerprint {
		t.Fatal("cookie change did not affect source fingerprint")
	}
}

func writeLegacyFixture(t *testing.T, root, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(root, "config.py"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

type fixedFingerprinter struct{}

func (fixedFingerprinter) Fingerprint(purpose string, body []byte) (string, error) {
	digest := hmac.New(sha256.New, []byte("01234567890123456789012345678901"))
	_, _ = digest.Write([]byte(purpose + "\x00"))
	_, _ = digest.Write(body)
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func inspectLegacyFixture(root string) (Plan, error) { return Inspect(root, fixedFingerprinter{}) }
