package integrations

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
)

const AdapterCatalogVersion = "upload-assistant.adapter-catalog.v1"

// AdapterCapability is the public, credential-free contract for one callable
// runtime boundary. The catalog also includes configured-but-not-callable site
// profiles so an operator or AI agent never has to infer support from a name.
type AdapterCapability struct {
	ID                string   `json:"id"`
	Kind              string   `json:"kind"`
	Adapter           string   `json:"adapter"`
	DisplayName       string   `json:"display_name"`
	SiteCode          string   `json:"site_code,omitempty"`
	RuntimeSupported  bool     `json:"runtime_supported"`
	Operations        []string `json:"operations"`
	CredentialFields  []string `json:"credential_fields"`
	SafetyGates       []string `json:"safety_gates"`
	Constraints       []string `json:"constraints"`
	UnavailableReason string   `json:"unavailable_reason,omitempty"`
}

type AdapterCatalog struct {
	Version  string              `json:"version"`
	SHA256   string              `json:"sha256"`
	Adapters []AdapterCapability `json:"adapters"`
}

// AdapterCapabilities returns a deterministic deep copy. Any change to a
// callable operation, credential field, gate, or constraint changes the
// catalog SHA-256 and the checked-in golden contract.
func AdapterCapabilities() AdapterCatalog {
	adapters := make([]AdapterCapability, len(adapterCatalog))
	for index, item := range adapterCatalog {
		adapters[index] = item
		adapters[index].Operations = append([]string{}, item.Operations...)
		adapters[index].CredentialFields = append([]string{}, item.CredentialFields...)
		adapters[index].SafetyGates = append([]string{}, item.SafetyGates...)
		adapters[index].Constraints = append([]string{}, item.Constraints...)
	}
	sort.Slice(adapters, func(left, right int) bool { return adapters[left].ID < adapters[right].ID })
	body, err := json.Marshal(struct {
		Version  string              `json:"version"`
		Adapters []AdapterCapability `json:"adapters"`
	}{Version: AdapterCatalogVersion, Adapters: adapters})
	if err != nil {
		panic(err)
	}
	digest := sha256.Sum256(body)
	return AdapterCatalog{Version: AdapterCatalogVersion, SHA256: hex.EncodeToString(digest[:]), Adapters: adapters}
}

var adapterCatalog = []AdapterCapability{
	{
		ID: "downloader/deluge", Kind: "downloader", Adapter: "deluge", DisplayName: "Deluge Web",
		RuntimeSupported: true, Operations: []string{"add_torrent", "inspect", "list_files", "probe", "set_limits", "wait_complete"},
		CredentialFields: []string{"password"}, SafetyGates: []string{"adapter_capability", "path_mapping", "rule_speed_limits", "verified_remote_receipt"},
		Constraints: []string{"Web JSON-RPC only; the Web session must already be connected to a daemon", "category, tags, skip_checking, and v2-only torrents are unsupported", "workflows must explicitly set apply_labels=false"},
	},
	{
		ID: "downloader/qbittorrent", Kind: "downloader", Adapter: "qbittorrent", DisplayName: "qBittorrent Web API",
		RuntimeSupported: true, Operations: []string{"add_torrent", "category", "inspect", "list_files", "probe", "set_limits", "skip_checking", "tags", "wait_complete"},
		CredentialFields: []string{"api_key", "password", "username"}, SafetyGates: []string{"adapter_capability", "path_mapping", "rule_speed_limits", "verified_remote_receipt"},
		Constraints: []string{"username and password must be supplied together when password authentication is used"},
	},
	{
		ID: "downloader/rtorrent", Kind: "downloader", Adapter: "rtorrent", DisplayName: "rTorrent XML-RPC",
		RuntimeSupported: true, Operations: []string{"add_torrent", "category", "inspect", "list_files", "probe", "set_limits", "tags", "wait_complete"},
		CredentialFields: []string{"password", "username"}, SafetyGates: []string{"adapter_capability", "path_mapping", "rule_speed_limits", "verified_remote_receipt"},
		Constraints: []string{"category and tags share the custom1 label", "named throttles are read back and must report an effective non-zero limit", "v2-only torrents and skip_checking are unsupported"},
	},
	{
		ID: "downloader/transmission", Kind: "downloader", Adapter: "transmission", DisplayName: "Transmission RPC",
		RuntimeSupported: true, Operations: []string{"add_torrent", "category", "inspect", "list_files", "probe", "set_limits", "tags", "wait_complete"},
		CredentialFields: []string{"password", "username"}, SafetyGates: []string{"adapter_capability", "path_mapping", "rule_speed_limits", "verified_remote_receipt"},
		Constraints: []string{"skip_checking is unsupported", "category and tags are represented with Transmission labels"},
	},
	{
		ID: "image_host/imgbb", Kind: "image_host", Adapter: "imgbb", DisplayName: "imgbb",
		RuntimeSupported: true, Operations: []string{"snapshot_configuration", "upload_image"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"bounded_image", "configuration_snapshot", "source_sha256", "upload_outcome_reconciliation", "verified_https_result"},
		Constraints: []string{"PNG, JPEG, or WebP up to 32 MiB", "redirects are disabled and returned image hosts are allowlisted"},
	},
	{
		ID: "image_host/ptpimg", Kind: "image_host", Adapter: "ptpimg", DisplayName: "PTPimg",
		RuntimeSupported: true, Operations: []string{"snapshot_configuration", "upload_image"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"bounded_image", "configuration_snapshot", "source_sha256", "upload_outcome_reconciliation", "verified_https_result"},
		Constraints: []string{"PNG, JPEG, or WebP up to 32 MiB", "redirects are disabled and returned images must use ptpimg.me"},
	},
	{
		ID: "media_analyzer/bdinfo", Kind: "media_analyzer", Adapter: "bdinfo", DisplayName: "BDInfoCLI",
		RuntimeSupported: true, Operations: []string{"analyze_bdmv"}, CredentialFields: []string{},
		SafetyGates: []string{"bounded_report", "input_path_policy", "tool_version_evidence"},
		Constraints: []string{"BDMV only", "each attempt uses an isolated report directory and accepts one bounded regular-text report"},
	},
	{
		ID: "media_analyzer/mediainfo", Kind: "media_analyzer", Adapter: "mediainfo", DisplayName: "MediaInfo",
		RuntimeSupported: true, Operations: []string{"analyze_media"}, CredentialFields: []string{},
		SafetyGates: []string{"bounded_output", "input_path_policy", "tool_version_evidence"},
		Constraints: []string{"regular media files only; BDMV is routed to BDInfo and VIDEO_TS remains blocked"},
	},
	{
		ID: "media_manager/radarr", Kind: "media_manager", Adapter: "radarr", DisplayName: "Radarr v3",
		RuntimeSupported: true, Operations: []string{"lookup", "probe"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"bounded_response", "explicit_external_read", "query_response_hashes"},
		Constraints: []string{"read-only API v3 operations", "local paths are represented only by a query hash in audit records"},
	},
	{
		ID: "media_manager/sonarr", Kind: "media_manager", Adapter: "sonarr", DisplayName: "Sonarr v3",
		RuntimeSupported: true, Operations: []string{"lookup", "probe"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"bounded_response", "explicit_external_read", "query_response_hashes"},
		Constraints: []string{"read-only API v3 operations", "local paths are represented only by a query hash in audit records"},
	},
	{
		ID: "metadata_provider/ptgen", Kind: "metadata_provider", Adapter: "ptgen", DisplayName: "PTGen",
		RuntimeSupported: true, Operations: []string{"resolve_douban", "resolve_imdb", "render_description"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"bounded_response", "explicit_external_read", "query_response_hashes"},
		Constraints: []string{"endpoint must be configured explicitly; no implicit public fallback", "api_key is optional only when the configured endpoint does not require it"},
	},
	{
		ID: "metadata_provider/tmdb", Kind: "metadata_provider", Adapter: "tmdb", DisplayName: "TMDb API v3",
		RuntimeSupported: true, Operations: []string{"resolve_external_ids", "resolve_imdb", "resolve_tmdb"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"bounded_response", "explicit_external_read", "identity_conflict_check", "query_response_hashes"},
		Constraints: []string{"ambiguous movie and TV matches are rejected", "redirects are disabled"},
	},
	{
		ID: "notification_channel/discord_webhook", Kind: "notification_channel", Adapter: "discord_webhook", DisplayName: "Discord Incoming Webhook",
		RuntimeSupported: true, Operations: []string{"deliver_candidate_summary", "reconcile_unknown_delivery", "retry_known_rejection"}, CredentialFields: []string{"webhook_url"},
		SafetyGates: []string{"delivery_intent_audit", "explicit_schedule_opt_in", "mentions_disabled", "outcome_reconciliation", "payload_hash", "remote_receipt_hash"},
		Constraints: []string{"candidate discovery notifications only; delivery never submits or uploads a torrent", "redirects are disabled and responses are bounded", "response loss and expired sending leases never auto-retry"},
	},
	{
		ID: "screenshot_engine/ffmpeg", Kind: "screenshot_engine", Adapter: "ffmpeg", DisplayName: "FFmpeg",
		RuntimeSupported: true, Operations: []string{"probe_duration", "render_screenshots"}, CredentialFields: []string{},
		SafetyGates: []string{"immutable_profile_revision", "input_path_policy", "output_sha256", "tool_version_evidence"},
		Constraints: []string{"PNG, JPEG, and WebP profiles only", "screenshot count, width, quality, and timeline bounds are validated before execution"},
	},
	{
		ID: "site/AUDIENCES", Kind: "site", Adapter: "nexusphp", DisplayName: "Audiences", SiteCode: "AUDIENCES",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/CHD", Kind: "site", Adapter: "nexusphp", DisplayName: "CHDBits", SiteCode: "CHD",
		RuntimeSupported: true, Operations: []string{"download_source_torrent", "inspect_source", "list_candidates"}, CredentialFields: []string{"cookie", "passkey"},
		SafetyGates: []string{"active_rule_revision", "download_permission", "source_reference_validation", "torrent_metainfo_validation"},
		Constraints: []string{"source-only reference workflow", "tracker redirects and login pages are rejected"},
	},
	{
		ID: "site/HDS", Kind: "site", Adapter: "nexusphp", DisplayName: "HD-Space", SiteCode: "HDS",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/HDSKY", Kind: "site", Adapter: "nexusphp", DisplayName: "HDSky", SiteCode: "HDSKY",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/HHAN", Kind: "site", Adapter: "nexusphp", DisplayName: "HhanClub", SiteCode: "HHAN",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/MTEAM", Kind: "site", Adapter: "mteam_api", DisplayName: "M-Team", SiteCode: "MTEAM",
		RuntimeSupported: true, Operations: []string{"download_uploaded_torrent", "duplicate_check", "prepare_upload_package", "upload_torrent"}, CredentialFields: []string{"api_key"},
		SafetyGates: []string{"accept_rules", "active_rule_revision", "confirm_upload", "duplicate_check", "manual_obligations", "upload_outcome_reconciliation"},
		Constraints: []string{"target-only reference workflow", "live upload requires exact rule fingerprints, resolved obligations, and explicit confirm_upload"},
	},
	{
		ID: "site/OB", Kind: "site", Adapter: "nexusphp", DisplayName: "OurBits", SiteCode: "OB",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/PTER", Kind: "site", Adapter: "nexusphp", DisplayName: "PterClub", SiteCode: "PTER",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/TJUPT", Kind: "site", Adapter: "nexusphp", DisplayName: "TJUPT", SiteCode: "TJUPT",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/TTG", Kind: "site", Adapter: "ttg", DisplayName: "ToTheGlory", SiteCode: "TTG",
		RuntimeSupported: false, Operations: []string{}, CredentialFields: []string{}, SafetyGates: []string{"active_rule_revision"}, Constraints: []string{},
		UnavailableReason: "configuration and rule revisions are supported, but no callable Go site adapter is registered",
	},
	{
		ID: "site/U2", Kind: "site", Adapter: "nexusphp", DisplayName: "U2分享園@動漫花園", SiteCode: "U2",
		RuntimeSupported: true, Operations: []string{"download_source_torrent", "inspect_source", "list_candidates"}, CredentialFields: []string{"cookie", "passkey"},
		SafetyGates: []string{"active_rule_revision", "download_permission", "source_reference_validation", "torrent_metainfo_validation"},
		Constraints: []string{"source-only reference workflow", "tracker redirects and login pages are rejected", "source torrent metainfo is never reused as the MTEAM target torrent"},
	},
	{
		ID: "torrent_maker/mkbrr", Kind: "torrent_maker", Adapter: "mkbrr", DisplayName: "mkbrr",
		RuntimeSupported: true, Operations: []string{"create_private_torrent", "inspect_output", "sanitize_output"}, CredentialFields: []string{},
		SafetyGates: []string{"content_manifest", "input_path_policy", "output_infohash", "output_sha256", "tool_version_evidence"},
		Constraints: []string{"target torrent is generated from completed local content", "announce URL is never exposed in API, logs, or reports"},
	},
}
