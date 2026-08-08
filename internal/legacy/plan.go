package legacy

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

const (
	legacyConfigFilename = "config.py"
	maxCookieBytes       = 2 << 20
	maxArchiveBytes      = 16 << 20
	archiveRetentionDays = 30
)

var (
	ErrSourceUnavailable = errors.New("legacy source is unavailable")
	ErrSourceInvalid     = errors.New("legacy source is invalid")
	legacyResourceName   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	secretPathSegment    = regexp.MustCompile(`(?i)^[a-z0-9_-]{24,}$`)
	legacySiteAllowlist  = []string{"AUDIENCES", "CHD", "HDS", "HDSKY", "HHAN", "MTEAM", "OB", "PTER", "TJUPT", "TTG", "U2"}
)

type Issue struct {
	Code     string `json:"code"`
	Message  string `json:"message"`
	Resource string `json:"resource,omitempty"`
}

type SourceFile struct {
	Path        string `json:"path"`
	Fingerprint string `json:"fingerprint"`
	SizeBytes   int64  `json:"size_bytes"`
}

type ResourcePreview struct {
	Kind             string         `json:"kind"`
	Name             string         `json:"name"`
	Adapter          string         `json:"adapter,omitempty"`
	Enabled          bool           `json:"enabled"`
	CredentialFields []string       `json:"credential_fields,omitempty"`
	Configuration    map[string]any `json:"configuration,omitempty"`
}

type ArchivePreview struct {
	Encrypted          bool  `json:"encrypted"`
	RetentionDays      int   `json:"retention_days"`
	FileCount          int   `json:"file_count"`
	UncompressedBytes  int64 `json:"uncompressed_bytes"`
	DeletesOriginals   bool  `json:"deletes_originals"`
	PlaintextAvailable bool  `json:"plaintext_available_via_api"`
}

type Preview struct {
	OK                bool              `json:"ok"`
	Status            string            `json:"status"`
	SourceKind        string            `json:"source_kind"`
	SourceFingerprint string            `json:"source_fingerprint"`
	SourceFiles       []SourceFile      `json:"source_files"`
	Resources         []ResourcePreview `json:"resources"`
	Archive           ArchivePreview    `json:"archive"`
	Blockers          []Issue           `json:"blockers"`
	Warnings          []Issue           `json:"warnings"`
	NextActions       []Issue           `json:"next_actions"`
}

type sourceBlob struct {
	path string
	body []byte
}

type siteCredentialOperation struct {
	siteCode string
	name     string
	value    string
}

type downloaderOperation struct {
	name  string
	input integrations.DownloaderInput
}

type imageHostOperation struct {
	name  string
	input integrations.ImageHostInput
}

type screenshotOperation struct {
	input integrations.ScreenshotProfileInput
}

type mediaManagerOperation struct {
	name  string
	input integrations.MediaManagerInput
}

// Plan contains an intentionally redacted public preview and private write
// operations. Secret-bearing fields are unexported so JSON and log encoders
// cannot expose legacy credentials by accident.
type Plan struct {
	Preview
	files         []sourceBlob
	sites         []siteCredentialOperation
	downloaders   []downloaderOperation
	imageHosts    []imageHostOperation
	screenshots   []screenshotOperation
	mediaManagers []mediaManagerOperation
}

type Fingerprinter interface {
	Fingerprint(string, []byte) (string, error)
}

func Inspect(root string, fingerprinter Fingerprinter) (Plan, error) {
	if fingerprinter == nil {
		return Plan{}, fmt.Errorf("%w: a keyed fingerprinter is required", ErrSourceInvalid)
	}
	root, err := validateLegacyRoot(root)
	if err != nil {
		return Plan{}, err
	}
	configBody, err := readRegularFile(root, legacyConfigFilename, maxConfigBytes)
	if err != nil {
		return Plan{}, err
	}
	parsed, err := ParseConfigLiteral(configBody)
	if err != nil {
		return Plan{}, fmt.Errorf("%w: %v", ErrSourceInvalid, err)
	}
	plan := Plan{Preview: Preview{
		SourceKind:  "upload_assistant_python_config",
		SourceFiles: []SourceFile{}, Resources: []ResourcePreview{},
		Blockers: []Issue{}, Warnings: []Issue{}, NextActions: []Issue{},
	}}
	plan.addSourceFile(legacyConfigFilename, configBody)
	if err := plan.readCookies(root); err != nil {
		return Plan{}, err
	}
	var sourceBytes int64
	for _, file := range plan.files {
		sourceBytes += int64(len(file.body))
		if sourceBytes > maxArchiveBytes {
			return Plan{}, fmt.Errorf("%w: legacy source exceeds the encrypted archive size limit", ErrSourceInvalid)
		}
	}
	if err := plan.build(parsed); err != nil {
		return Plan{}, fmt.Errorf("%w: %v", ErrSourceInvalid, err)
	}
	if err := plan.finalizeFingerprints(fingerprinter); err != nil {
		return Plan{}, err
	}
	var total int64
	for _, file := range plan.SourceFiles {
		total += file.SizeBytes
	}
	plan.Archive = ArchivePreview{
		Encrypted: true, RetentionDays: archiveRetentionDays, FileCount: len(plan.files),
		UncompressedBytes: total, DeletesOriginals: false, PlaintextAvailable: false,
	}
	plan.OK = len(plan.Blockers) == 0 && len(plan.Resources) > 0
	if plan.OK {
		plan.Status = "ready"
		plan.NextActions = append(plan.NextActions, Issue{
			Code: "confirm_legacy_import", Message: "提交 source_fingerprint 并显式确认后才能执行迁移。",
		})
	} else {
		plan.Status = "blocked"
		if len(plan.Resources) == 0 {
			plan.Blockers = append(plan.Blockers, Issue{Code: "no_supported_resources", Message: "旧配置中没有可安全迁移到 Go 服务的受支持资源。"})
		}
	}
	return plan, nil
}

func validateLegacyRoot(root string) (string, error) {
	root = filepath.Clean(strings.TrimSpace(root))
	if root == "." || !filepath.IsAbs(root) {
		return "", fmt.Errorf("%w: legacy directory must be absolute", ErrSourceInvalid)
	}
	info, err := os.Lstat(root)
	if err != nil {
		return "", fmt.Errorf("%w: legacy directory cannot be read", ErrSourceUnavailable)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return "", fmt.Errorf("%w: legacy directory must be a real directory", ErrSourceInvalid)
	}
	return root, nil
}

func readRegularFile(root, relative string, limit int64) ([]byte, error) {
	path := filepath.Join(root, filepath.FromSlash(relative))
	info, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("%w: required legacy file %s is missing", ErrSourceUnavailable, relative)
		}
		return nil, fmt.Errorf("%w: legacy file %s cannot be inspected", ErrSourceUnavailable, relative)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%w: legacy file %s must be a regular non-symlink file", ErrSourceInvalid, relative)
	}
	if info.Size() <= 0 || info.Size() > limit {
		return nil, fmt.Errorf("%w: legacy file %s has an invalid size", ErrSourceInvalid, relative)
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("%w: legacy file %s cannot be read", ErrSourceUnavailable, relative)
	}
	defer file.Close()
	openedInfo, err := file.Stat()
	if err != nil || !openedInfo.Mode().IsRegular() || !os.SameFile(info, openedInfo) {
		return nil, fmt.Errorf("%w: legacy file %s changed while it was opened", ErrSourceInvalid, relative)
	}
	body, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, fmt.Errorf("%w: legacy file %s cannot be read", ErrSourceUnavailable, relative)
	}
	if int64(len(body)) != info.Size() || int64(len(body)) > limit {
		return nil, fmt.Errorf("%w: legacy file %s changed while it was read", ErrSourceInvalid, relative)
	}
	return body, nil
}

func (plan *Plan) readCookies(root string) error {
	cookiesPath := filepath.Join(root, "cookies")
	info, err := os.Lstat(cookiesPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("%w: cookies directory cannot be inspected", ErrSourceUnavailable)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("%w: cookies must be a real directory", ErrSourceInvalid)
	}
	for _, site := range legacySiteAllowlist {
		relative := "cookies/" + site + ".txt"
		body, err := readOptionalRegularFile(root, relative, maxCookieBytes)
		if err != nil {
			return err
		}
		if len(body) == 0 {
			continue
		}
		if !utf8.Valid(body) || strings.ContainsRune(string(body), '\x00') {
			return fmt.Errorf("%w: cookie file %s is not valid text", ErrSourceInvalid, relative)
		}
		plan.addSourceFile(relative, body)
		if strings.TrimSpace(string(body)) != "" {
			plan.sites = append(plan.sites, siteCredentialOperation{siteCode: site, name: "cookie", value: string(body)})
			plan.Resources = append(plan.Resources, ResourcePreview{
				Kind: "site_credential", Name: site + ".cookie", Enabled: true,
				CredentialFields: []string{"cookie"},
			})
		}
	}
	return nil
}

func readOptionalRegularFile(root, relative string, limit int64) ([]byte, error) {
	path := filepath.Join(root, filepath.FromSlash(relative))
	if _, err := os.Lstat(path); errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	return readRegularFile(root, relative, limit)
}

func (plan *Plan) addSourceFile(path string, body []byte) {
	copyBody := append([]byte(nil), body...)
	plan.files = append(plan.files, sourceBlob{path: filepath.ToSlash(path), body: copyBody})
	plan.SourceFiles = append(plan.SourceFiles, SourceFile{
		Path: filepath.ToSlash(path), SizeBytes: int64(len(copyBody)),
	})
}

func (plan *Plan) finalizeFingerprints(fingerprinter Fingerprinter) error {
	for index, file := range plan.files {
		fingerprint, err := fingerprinter.Fingerprint("legacy.source.file.v1:"+file.path, file.body)
		if err != nil {
			return fmt.Errorf("fingerprint legacy source file: %w", err)
		}
		plan.SourceFiles[index].Fingerprint = fingerprint
	}
	ordered := append([]sourceBlob(nil), plan.files...)
	slices.SortFunc(ordered, func(left, right sourceBlob) int { return strings.Compare(left.path, right.path) })
	var material bytes.Buffer
	for _, file := range ordered {
		_, _ = fmt.Fprintf(&material, "%d:%s:%d:", len(file.path), file.path, len(file.body))
		_, _ = material.Write(file.body)
	}
	fingerprint, err := fingerprinter.Fingerprint("legacy.source.bundle.v1", material.Bytes())
	if err != nil {
		return fmt.Errorf("fingerprint legacy source bundle: %w", err)
	}
	plan.SourceFingerprint = fingerprint
	return nil
}

func (plan *Plan) build(config map[string]any) error {
	defaults, err := optionalSection(config, "DEFAULT")
	if err != nil {
		return err
	}
	trackers, err := optionalSection(config, "TRACKERS")
	if err != nil {
		return err
	}
	clients, err := optionalSection(config, "TORRENT_CLIENTS")
	if err != nil {
		return err
	}
	plan.buildSites(trackers)
	plan.buildDownloaders(defaults, clients)
	plan.buildImageHosts(defaults)
	plan.buildScreenshots(defaults)
	plan.buildMediaManagers(defaults)
	plan.reportUnsupported(defaults, config, trackers)
	return nil
}

func optionalSection(config map[string]any, name string) (map[string]any, error) {
	value, ok := config[name]
	if !ok || value == nil {
		return map[string]any{}, nil
	}
	section, ok := value.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s must be a dictionary", name)
	}
	return section, nil
}

func (plan *Plan) buildSites(trackers map[string]any) {
	existingCookie := map[string]bool{}
	for _, operation := range plan.sites {
		existingCookie[operation.siteCode] = true
	}
	for _, site := range legacySiteAllowlist {
		section, ok := trackers[site].(map[string]any)
		if !ok {
			continue
		}
		keys := []string{"passkey"}
		if site == "MTEAM" {
			keys = []string{"api_key"}
		}
		for _, key := range keys {
			value := usefulString(section[key])
			if value == "" {
				continue
			}
			plan.sites = append(plan.sites, siteCredentialOperation{siteCode: site, name: key, value: value})
			plan.Resources = append(plan.Resources, ResourcePreview{
				Kind: "site_credential", Name: site + "." + key, Enabled: true,
				CredentialFields: []string{key},
			})
		}
		if site != "MTEAM" && !existingCookie[site] {
			if usefulString(section["username"]) != "" || usefulString(section["password"]) != "" {
				plan.Warnings = append(plan.Warnings, Issue{
					Code: "legacy_login_not_imported", Resource: site,
					Message: "用户名/密码不会迁移；NexusPHP 运行时需要单独提供 cookie，避免自动登录。",
				})
			}
		}
	}
}

func (plan *Plan) buildDownloaders(defaults, clients map[string]any) {
	defaultName := usefulString(defaults["default_torrent_client"])
	for name, raw := range clients {
		section, ok := raw.(map[string]any)
		if !ok {
			plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_downloader_skipped", Resource: name, Message: "下载器配置不是字典，已跳过。"})
			continue
		}
		if !legacyResourceName.MatchString(name) {
			plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_downloader_name_invalid", Resource: "downloader", Message: "存在名称不符合新服务约束的下载器，已跳过。"})
			continue
		}
		clientType := strings.ToLower(usefulString(section["torrent_client"]))
		if clientType == "deluge" {
			plan.Warnings = append(plan.Warnings, Issue{
				Code: "legacy_deluge_web_endpoint_required", Resource: name,
				Message: "旧配置使用 Deluge 原生 daemon RPC 地址/账号，不能安全转换为 Deluge Web JSON-RPC；请在 Web 配置中心手工填写 Web endpoint 与 Web 密码。",
			})
			continue
		}
		if clientType == "transmission" {
			plan.buildTransmissionDownloader(defaultName, name, section)
			continue
		}
		if clientType == "rtorrent" {
			plan.buildRTorrentDownloader(defaultName, name, section)
			continue
		}
		if clientType != "qbit" && clientType != "qbittorrent" {
			if clientType != "" {
				plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_downloader_adapter_deferred", Resource: name, Message: "当前 Go 运行时尚未执行该下载器；配置需在对应 adapter 完成后手工迁移。"})
			}
			continue
		}
		if usefulString(section["qui_proxy_url"]) != "" {
			plan.Warnings = append(plan.Warnings, Issue{Code: "qui_proxy_requires_manual_configuration", Resource: name, Message: "QUI proxy URL 可能把密钥编码在路径中，不能安全自动迁移。"})
			continue
		}
		endpoint, loopback, endpointErr := legacyQBitEndpoint(section)
		if endpointErr != nil {
			plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_qbittorrent_endpoint_invalid", Resource: name, Message: "qBittorrent 地址无法安全转换，已跳过。"})
			continue
		}
		credentials := map[string]string{}
		username, password := usefulString(section["qbit_user"]), usefulString(section["qbit_pass"])
		if username != "" && password != "" {
			credentials["username"], credentials["password"] = username, password
		} else if username != "" || password != "" {
			plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_qbittorrent_credentials_incomplete", Resource: name, Message: "qBittorrent 用户名和密码不完整；配置会保持禁用。"})
		}
		enabled := (defaultName == "" || name == defaultName) && !loopback && len(credentials) == 2
		if loopback {
			plan.Warnings = append(plan.Warnings, Issue{Code: "container_loopback_requires_review", Resource: name, Message: "旧地址指向 127.0.0.1/localhost，在容器中不是盒子宿主机；配置会保持禁用。"})
		}
		mappings, mappingWarnings := legacyPathMappings(section)
		plan.Warnings = append(plan.Warnings, mappingWarnings...)
		options := map[string]any{}
		for legacyKey, currentKey := range map[string]string{"qbit_cat": "category", "qbit_tag": "tag"} {
			if value := usefulString(section[legacyKey]); value != "" {
				options[currentKey] = value
			}
		}
		plan.downloaders = append(plan.downloaders, downloaderOperation{name: name, input: integrations.DownloaderInput{
			Adapter: "qbittorrent", Enabled: boolPointer(enabled),
			Config:      integrations.EndpointConfig{Endpoint: endpoint, TimeoutSeconds: 30, Options: options},
			Credentials: credentials, PathMappings: mappings,
		}})
		fields := sortedKeys(credentials)
		plan.Resources = append(plan.Resources, ResourcePreview{
			Kind: "downloader", Name: name, Adapter: "qbittorrent", Enabled: enabled,
			CredentialFields: fields,
			Configuration:    map[string]any{"endpoint": endpoint, "path_mapping_count": len(mappings), "options": options},
		})
		if section["qbit_download_limit"] != nil || section["qbit_upload_limit"] != nil {
			plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_speed_limits_require_rule_review", Resource: name, Message: "旧下载器限速不会覆盖已审批站点规则，需在规则 Markdown 中人工复核。"})
		}
	}
}

func (plan *Plan) buildRTorrentDownloader(defaultName, name string, section map[string]any) {
	endpoint, credentials, credentialsComplete, loopback, err := legacyRTorrentEndpoint(section)
	if err != nil {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_rtorrent_endpoint_invalid", Resource: name, Message: "rTorrent XML-RPC 地址无法安全转换，已跳过。"})
		return
	}
	if !credentialsComplete {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_rtorrent_credentials_incomplete", Resource: name, Message: "rTorrent URL 中的用户名和密码不完整；凭据不会迁移，配置会保持禁用。"})
	}
	enabled := (defaultName == "" || name == defaultName) && !loopback && credentialsComplete
	if loopback {
		plan.Warnings = append(plan.Warnings, Issue{Code: "container_loopback_requires_review", Resource: name, Message: "旧地址指向 127.0.0.1/localhost，在容器中不是盒子宿主机；配置会保持禁用。"})
	}
	mappings, mappingWarnings := legacyPathMappings(section)
	plan.Warnings = append(plan.Warnings, mappingWarnings...)
	options := map[string]any{}
	if label := usefulString(section["rtorrent_label"]); label != "" {
		options["label"] = label
	}
	if usefulString(section["torrent_storage_dir"]) != "" {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_rtorrent_session_path_not_imported", Resource: name, Message: "旧 rTorrent session 目录不会迁移；新运行时只通过 XML-RPC 读取可验证状态。"})
	}
	plan.downloaders = append(plan.downloaders, downloaderOperation{name: name, input: integrations.DownloaderInput{
		Adapter: "rtorrent", Enabled: boolPointer(enabled),
		Config:      integrations.EndpointConfig{Endpoint: endpoint, TimeoutSeconds: 30, Options: options},
		Credentials: credentials, PathMappings: mappings,
	}})
	plan.Resources = append(plan.Resources, ResourcePreview{
		Kind: "downloader", Name: name, Adapter: "rtorrent", Enabled: enabled,
		CredentialFields: sortedKeys(credentials),
		Configuration:    map[string]any{"endpoint": endpoint, "path_mapping_count": len(mappings), "options": options},
	})
}

func legacyRTorrentEndpoint(section map[string]any) (string, map[string]string, bool, bool, error) {
	raw := usefulString(section["rtorrent_url"])
	if raw == "" {
		return "", nil, false, false, errors.New("rtorrent_url is empty")
	}
	parsed, err := url.Parse(raw)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", nil, false, false, errors.New("rtorrent_url is unsafe")
	}
	credentials := map[string]string{}
	credentialsComplete := true
	if parsed.User != nil {
		username := strings.TrimSpace(parsed.User.Username())
		password, hasPassword := parsed.User.Password()
		if username != "" && hasPassword && password != "" {
			credentials["username"], credentials["password"] = username, password
		} else {
			credentialsComplete = false
		}
		parsed.User = nil
	}
	for _, segment := range strings.Split(strings.Trim(parsed.Path, "/"), "/") {
		if segment == "." || segment == ".." || strings.ContainsAny(segment, "\x00\r\n") || secretPathSegment.MatchString(segment) {
			return "", nil, false, false, errors.New("rtorrent_url path is unsafe")
		}
	}
	hostname := strings.ToLower(parsed.Hostname())
	loopback := hostname == "localhost"
	if address := net.ParseIP(hostname); address != nil && address.IsLoopback() {
		loopback = true
	}
	return parsed.String(), credentials, credentialsComplete, loopback, nil
}

func (plan *Plan) buildTransmissionDownloader(defaultName, name string, section map[string]any) {
	endpoint, loopback, err := legacyTransmissionEndpoint(section)
	if err != nil {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_transmission_endpoint_invalid", Resource: name, Message: "Transmission RPC 地址无法安全转换，已跳过。"})
		return
	}
	credentials := map[string]string{}
	username := usefulString(section["transmission_username"])
	password := usefulString(section["transmission_password"])
	credentialsComplete := username == "" && password == ""
	if username != "" && password != "" {
		credentials["username"], credentials["password"] = username, password
		credentialsComplete = true
	} else if username != "" || password != "" {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_transmission_credentials_incomplete", Resource: name, Message: "Transmission 用户名和密码不完整；配置会保持禁用。"})
	}
	enabled := (defaultName == "" || name == defaultName) && !loopback && credentialsComplete
	if loopback {
		plan.Warnings = append(plan.Warnings, Issue{Code: "container_loopback_requires_review", Resource: name, Message: "旧地址指向 127.0.0.1/localhost，在容器中不是盒子宿主机；配置会保持禁用。"})
	}
	mappings, mappingWarnings := legacyPathMappings(section)
	plan.Warnings = append(plan.Warnings, mappingWarnings...)
	options := map[string]any{}
	if label := usefulString(section["transmission_label"]); label != "" {
		options["label"] = label
	}
	plan.downloaders = append(plan.downloaders, downloaderOperation{name: name, input: integrations.DownloaderInput{
		Adapter: "transmission", Enabled: boolPointer(enabled),
		Config:      integrations.EndpointConfig{Endpoint: endpoint, TimeoutSeconds: 30, Options: options},
		Credentials: credentials, PathMappings: mappings,
	}})
	plan.Resources = append(plan.Resources, ResourcePreview{
		Kind: "downloader", Name: name, Adapter: "transmission", Enabled: enabled,
		CredentialFields: sortedKeys(credentials),
		Configuration:    map[string]any{"endpoint": endpoint, "path_mapping_count": len(mappings), "options": options},
	})
}

func legacyTransmissionEndpoint(section map[string]any) (string, bool, error) {
	protocol := strings.ToLower(usefulString(section["transmission_protocol"]))
	if protocol == "" {
		protocol = "http"
	}
	if protocol != "http" && protocol != "https" {
		return "", false, errors.New("transmission_protocol is invalid")
	}
	host := usefulString(section["transmission_host"])
	if host == "" || strings.ContainsAny(host, "/?#@") {
		return "", false, errors.New("transmission_host is invalid")
	}
	parsed, err := url.Parse(protocol + "://" + host)
	if err != nil || parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Path != "" {
		return "", false, errors.New("transmission_host is unsafe")
	}
	port := 9091
	if rawPort, exists := section["transmission_port"]; exists && rawPort != nil {
		value, err := integerValue(rawPort)
		if err != nil || value < 1 || value > 65535 {
			return "", false, errors.New("transmission_port is invalid")
		}
		port = value
	}
	portText := strconv.Itoa(port)
	if parsed.Port() != "" && parsed.Port() != portText {
		return "", false, errors.New("transmission ports conflict")
	}
	parsed.Host = net.JoinHostPort(parsed.Hostname(), portText)
	rpcPath := usefulString(section["transmission_path"])
	if rpcPath == "" {
		rpcPath = "/transmission/rpc"
	}
	if !strings.HasPrefix(rpcPath, "/") || filepath.Clean(rpcPath) != rpcPath {
		return "", false, errors.New("transmission_path is invalid")
	}
	for _, segment := range strings.Split(strings.Trim(rpcPath, "/"), "/") {
		if secretPathSegment.MatchString(segment) {
			return "", false, errors.New("transmission_path may contain a credential")
		}
	}
	parsed.Path = rpcPath
	hostname := strings.ToLower(parsed.Hostname())
	loopback := hostname == "localhost"
	if address := net.ParseIP(hostname); address != nil && address.IsLoopback() {
		loopback = true
	}
	return parsed.String(), loopback, nil
}

func legacyQBitEndpoint(section map[string]any) (string, bool, error) {
	raw := usefulString(section["qbit_url"])
	if raw == "" {
		return "", false, errors.New("qbit_url is empty")
	}
	parsed, err := url.Parse(strings.TrimRight(raw, "/"))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", false, errors.New("qbit_url is unsafe")
	}
	for _, segment := range strings.Split(parsed.EscapedPath(), "/") {
		if secretPathSegment.MatchString(segment) {
			return "", false, errors.New("qbit_url path may contain a credential")
		}
	}
	port := usefulString(section["qbit_port"])
	if parsed.Port() == "" && port != "" {
		value, err := strconv.Atoi(port)
		if err != nil || value < 1 || value > 65535 {
			return "", false, errors.New("qbit_port is invalid")
		}
		parsed.Host = net.JoinHostPort(parsed.Hostname(), port)
	}
	host := strings.ToLower(parsed.Hostname())
	loopback := host == "localhost"
	if address := net.ParseIP(host); address != nil && address.IsLoopback() {
		loopback = true
	}
	return strings.TrimRight(parsed.String(), "/"), loopback, nil
}

func legacyPathMappings(section map[string]any) ([]integrations.PathMapping, []Issue) {
	locals, localOK := stringSequence(section["local_path"])
	remotes, remoteOK := stringSequence(section["remote_path"])
	if !localOK || !remoteOK {
		return nil, []Issue{{Code: "legacy_path_mapping_invalid", Resource: "downloader", Message: "local_path/remote_path 必须是字符串或字符串列表；路径映射已跳过。"}}
	}
	locals, remotes = usefulStrings(locals), usefulStrings(remotes)
	if len(locals) == 0 && len(remotes) == 0 {
		return []integrations.PathMapping{}, nil
	}
	if len(locals) != len(remotes) {
		return nil, []Issue{{Code: "legacy_path_mapping_mismatch", Resource: "downloader", Message: "local_path 与 remote_path 数量不一致；路径映射已跳过。"}}
	}
	result := make([]integrations.PathMapping, 0, len(locals))
	for index := range locals {
		if !filepath.IsAbs(locals[index]) || !filepath.IsAbs(remotes[index]) {
			return nil, []Issue{{Code: "legacy_path_mapping_relative", Resource: "downloader", Message: "路径映射包含相对路径；全部映射已跳过。"}}
		}
		result = append(result, integrations.PathMapping{RemotePath: remotes[index], LocalPath: locals[index], Priority: 100 - index})
	}
	return result, nil
}

func (plan *Plan) buildImageHosts(defaults map[string]any) {
	priorities := map[string]int{}
	for index := 1; index <= 6; index++ {
		name := strings.ToLower(usefulString(defaults[fmt.Sprintf("img_host_%d", index)]))
		if name != "" {
			priorities[name] = index * 100
		}
	}
	for _, definition := range []struct {
		name, key, endpoint string
	}{
		{name: "imgbb", key: "imgbb_api", endpoint: "https://api.imgbb.com/1/upload"},
		{name: "ptpimg", key: "ptpimg_api", endpoint: "https://ptpimg.me/upload.php"},
	} {
		apiKey := usefulString(defaults[definition.key])
		if apiKey == "" {
			continue
		}
		priority := priorities[definition.name]
		if priority == 0 {
			priority = 1000
		}
		enabled := true
		plan.imageHosts = append(plan.imageHosts, imageHostOperation{name: definition.name, input: integrations.ImageHostInput{
			Adapter: definition.name, Enabled: &enabled, Priority: priority,
			Config:      integrations.EndpointConfig{Endpoint: definition.endpoint, TimeoutSeconds: 90, Options: map[string]any{}},
			Credentials: map[string]string{"api_key": apiKey},
		}})
		plan.Resources = append(plan.Resources, ResourcePreview{
			Kind: "image_host", Name: definition.name, Adapter: definition.name, Enabled: true,
			CredentialFields: []string{"api_key"}, Configuration: map[string]any{"endpoint": definition.endpoint, "priority": priority},
		})
	}
}

func (plan *Plan) buildScreenshots(defaults map[string]any) {
	raw, exists := defaults["screens"]
	if !exists || raw == nil {
		return
	}
	count, err := integerValue(raw)
	if err != nil || count < 1 || count > 20 {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_screenshot_count_invalid", Resource: "legacy-default", Message: "截图数量不在 1 到 20 之间，截图配置已跳过。"})
		return
	}
	enabled := true
	config := map[string]any{
		"count": count, "format": "png", "quality": 90,
		"start_percent": 0.1, "end_percent": 0.9, "comparison": false,
	}
	plan.screenshots = append(plan.screenshots, screenshotOperation{input: integrations.ScreenshotProfileInput{Name: "legacy-default", Enabled: &enabled, Config: config}})
	plan.Resources = append(plan.Resources, ResourcePreview{Kind: "screenshot_profile", Name: "legacy-default", Enabled: true, Configuration: config})
}

func (plan *Plan) buildMediaManagers(defaults map[string]any) {
	for _, adapter := range []string{"sonarr", "radarr"} {
		useEnabled, _ := defaults["use_"+adapter].(bool)
		for index := 0; index < 4; index++ {
			suffix := ""
			name := adapter
			if index > 0 {
				suffix = "_" + strconv.Itoa(index)
				name = adapter + "-" + strconv.Itoa(index)
			}
			endpointValue := usefulString(defaults[adapter+"_url"+suffix])
			apiKey := usefulString(defaults[adapter+"_api_key"+suffix])
			if endpointValue == "" && apiKey == "" {
				continue
			}
			if endpointValue == "" || apiKey == "" {
				plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_media_manager_incomplete", Resource: name, Message: "旧媒体管理器的 endpoint 或 API key 不完整，已跳过。"})
				continue
			}
			endpoint, loopback, err := safeLegacyServiceEndpoint(endpointValue)
			if err != nil {
				plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_media_manager_endpoint_invalid", Resource: name, Message: "旧媒体管理器地址无法安全转换，已跳过。"})
				continue
			}
			enabled := useEnabled && !loopback
			if loopback {
				plan.Warnings = append(plan.Warnings, Issue{Code: "container_loopback_requires_review", Resource: name, Message: "旧媒体管理器地址指向 127.0.0.1/localhost，在容器中不是盒子宿主机；配置会保持禁用。"})
			}
			plan.mediaManagers = append(plan.mediaManagers, mediaManagerOperation{name: name, input: integrations.MediaManagerInput{
				Adapter: adapter, Enabled: boolPointer(enabled), Config: integrations.EndpointConfig{Endpoint: endpoint, TimeoutSeconds: 15, Options: map[string]any{}},
				Credentials: map[string]string{"api_key": apiKey},
			}})
			plan.Resources = append(plan.Resources, ResourcePreview{Kind: "media_manager", Name: name, Adapter: adapter, Enabled: enabled,
				CredentialFields: []string{"api_key"}, Configuration: map[string]any{"endpoint": endpoint}})
		}
	}
}

func safeLegacyServiceEndpoint(value string) (string, bool, error) {
	parsed, err := url.Parse(strings.TrimRight(strings.TrimSpace(value), "/"))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", false, errors.New("unsafe service endpoint")
	}
	if decodedPath, err := url.PathUnescape(parsed.EscapedPath()); err != nil || (decodedPath != "" && filepath.Clean(decodedPath) != decodedPath) {
		return "", false, errors.New("unsafe service endpoint path")
	} else {
		for _, segment := range strings.Split(decodedPath, "/") {
			if segment == ".." || segment == "." || strings.ContainsAny(segment, "\x00\r\n") {
				return "", false, errors.New("unsafe service endpoint path")
			}
		}
	}
	host := strings.ToLower(parsed.Hostname())
	loopback := host == "localhost"
	if address := net.ParseIP(host); address != nil && address.IsLoopback() {
		loopback = true
	}
	return parsed.String(), loopback, nil
}

func (plan *Plan) reportUnsupported(defaults, config, trackers map[string]any) {
	for _, key := range []string{"tmdb_api", "btn_api"} {
		if usefulString(defaults[key]) != "" {
			plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_metadata_credential_deferred", Resource: key, Message: "元数据凭据尚无对应的 Go 配置资源，已仅保留在 30 天加密归档中。"})
		}
	}
	if toneMap, ok := defaults["tone_map"].(bool); ok && toneMap {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_tonemap_requires_manual_configuration", Resource: "legacy-default", Message: "旧 tone_map 设置不会自动映射，需按当前截图工具能力人工配置。"})
	}
	if value, ok := config["DISCORD"].(map[string]any); ok && hasUsefulValue(value) {
		plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_discord_bot_requires_webhook", Resource: "discord", Message: "旧 Discord bot token/频道不能安全转换为 incoming webhook；请在 Web 配置中心新建 webhook 渠道。"})
	}
	for site, raw := range trackers {
		section, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if section["qbit_download_limit"] != nil || section["qbit_upload_limit"] != nil {
			plan.Warnings = append(plan.Warnings, Issue{Code: "site_rule_limits_require_manual_review", Resource: strings.ToUpper(site), Message: "站点限速属于可审计规则，不会从旧 Python 配置静默写入；请更新并审批规则 Markdown。"})
		}
		for _, key := range []string{"ptgen_api", "ids_moe_api_key", "announce_url"} {
			if usefulString(section[key]) != "" {
				plan.Warnings = append(plan.Warnings, Issue{Code: "legacy_site_field_deferred", Resource: strings.ToUpper(site) + "." + key, Message: "该字段未被当前 Go 站点 adapter 消费，已仅保留在 30 天加密归档中。"})
			}
		}
	}
}

func usefulString(value any) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	text = strings.TrimSpace(text)
	lower := strings.ToLower(text)
	if text == "" || strings.HasPrefix(text, "${") || strings.Contains(lower, "<pass") || strings.Contains(lower, "<your_") {
		return ""
	}
	for _, placeholder := range []string{"passkey", "password", "username", "api_key", "api key", "ptp api key", "ptp api user", "custom_announce_url"} {
		if lower == placeholder {
			return ""
		}
	}
	return text
}

func stringSequence(value any) ([]string, bool) {
	if value == nil {
		return []string{}, true
	}
	if text, ok := value.(string); ok {
		return []string{text}, true
	}
	items, ok := value.([]any)
	if !ok {
		return nil, false
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok {
			return nil, false
		}
		result = append(result, text)
	}
	return result, true
}

func usefulStrings(values []string) []string {
	result := make([]string, 0, len(values))
	for _, value := range values {
		if value = usefulString(value); value != "" {
			result = append(result, filepath.Clean(value))
		}
	}
	return result
}

func integerValue(value any) (int, error) {
	switch typed := value.(type) {
	case int64:
		return int(typed), nil
	case string:
		return strconv.Atoi(strings.TrimSpace(typed))
	default:
		return 0, errors.New("not an integer")
	}
}

func sortedKeys(values map[string]string) []string {
	result := make([]string, 0, len(values))
	for key := range values {
		result = append(result, key)
	}
	slices.Sort(result)
	return result
}

func boolPointer(value bool) *bool { return &value }

func hasUsefulValue(values map[string]any) bool {
	for _, value := range values {
		switch typed := value.(type) {
		case string:
			if usefulString(typed) != "" {
				return true
			}
		case bool:
			if typed {
				return true
			}
		case int64:
			if typed != 0 {
				return true
			}
		case map[string]any:
			if hasUsefulValue(typed) {
				return true
			}
		case []any:
			if len(typed) > 0 {
				return true
			}
		}
	}
	return false
}
