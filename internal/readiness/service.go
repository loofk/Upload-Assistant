package readiness

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"slices"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/rules"
)

var ErrInvalid = errors.New("live readiness input is invalid")

var resourceNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

type RuleProvider interface {
	Active(context.Context, string) (rules.Revision, error)
}

type IntegrationProvider interface {
	GetRuntimeSite(context.Context, string) (integrations.RuntimeSite, error)
	GetRuntimeDownloader(context.Context, string) (integrations.RuntimeDownloader, error)
	GetRuntimeImageHost(context.Context, string) (integrations.RuntimeImageHost, error)
	GetRuntimeScreenshotProfile(context.Context, string) (integrations.RuntimeScreenshotProfile, error)
}

type Runtime struct {
	MediaInfoBinary string
	FFmpegBinary    string
	FFprobeBinary   string
	MkbrrBinary     string
	DownloadsDir    string
}

type Input struct {
	Source            string
	Target            string
	Downloader        string
	TargetDownloader  string
	ImageHost         string
	ScreenshotProfile string
}

type Check struct {
	Key      string         `json:"key"`
	Status   string         `json:"status"`
	Summary  string         `json:"summary"`
	Evidence map[string]any `json:"evidence,omitempty"`
}

type Blocker struct {
	Code      string         `json:"code"`
	Message   string         `json:"message"`
	Component string         `json:"component"`
	Details   map[string]any `json:"details,omitempty"`
}

type NextAction struct {
	Action      string         `json:"action"`
	Description string         `json:"description"`
	Parameters  map[string]any `json:"parameters,omitempty"`
}

type RuleConfirmation struct {
	SiteCode      string   `json:"site_code"`
	Fingerprint   string   `json:"fingerprint"`
	ObligationIDs []string `json:"obligation_ids"`
}

type Report struct {
	OK                     bool               `json:"ok"`
	Status                 string             `json:"status"`
	ConfigurationReady     bool               `json:"configuration_ready"`
	ExternalCallsPerformed bool               `json:"external_calls_performed"`
	LiveUploadAuthorized   bool               `json:"live_upload_authorized"`
	Source                 string             `json:"source"`
	Target                 string             `json:"target"`
	Checks                 []Check            `json:"checks"`
	RequiredConfirmations  []RuleConfirmation `json:"required_confirmations"`
	Blockers               []Blocker          `json:"blockers"`
	NextActions            []NextAction       `json:"next_actions"`
	ResumeState            map[string]any     `json:"resume_state"`
	Summary                string             `json:"summary"`
}

type Service struct {
	rules        RuleProvider
	integrations IntegrationProvider
	runtime      Runtime
}

func NewService(ruleProvider RuleProvider, integrationProvider IntegrationProvider, runtime Runtime) *Service {
	if strings.TrimSpace(runtime.DownloadsDir) == "" {
		runtime.DownloadsDir = "/downloads"
	}
	return &Service{rules: ruleProvider, integrations: integrationProvider, runtime: runtime}
}

func (service *Service) Check(ctx context.Context, input Input) (Report, error) {
	input.Source = strings.ToUpper(strings.TrimSpace(input.Source))
	input.Target = strings.ToUpper(strings.TrimSpace(input.Target))
	input.Downloader = strings.TrimSpace(input.Downloader)
	input.TargetDownloader = strings.TrimSpace(input.TargetDownloader)
	input.ImageHost = strings.TrimSpace(input.ImageHost)
	input.ScreenshotProfile = strings.TrimSpace(input.ScreenshotProfile)
	if input.TargetDownloader == "" {
		input.TargetDownloader = input.Downloader
	}
	if err := validateInput(input); err != nil {
		return Report{}, err
	}
	report := Report{
		Status: "blocked", ExternalCallsPerformed: false, LiveUploadAuthorized: false,
		Source: input.Source, Target: input.Target, Checks: []Check{}, RequiredConfirmations: []RuleConfirmation{},
		Blockers: []Blocker{}, NextActions: []NextAction{}, ResumeState: map[string]any{"accept_rules": map[string]any{}, "confirm_upload": false},
	}

	service.checkAdapter(&report, "source_adapter", input.Source, slices.Contains([]string{"U2", "CHD"}, input.Source), "source")
	service.checkAdapter(&report, "target_adapter", input.Target, input.Target == "MTEAM", "target")
	if err := service.checkRules(ctx, &report, input.Source); err != nil {
		return Report{}, err
	}
	if err := service.checkRules(ctx, &report, input.Target); err != nil {
		return Report{}, err
	}
	if err := service.checkSite(ctx, &report, input.Source, "nexusphp", []string{"cookie", "passkey"}); err != nil {
		return Report{}, err
	}
	if err := service.checkSite(ctx, &report, input.Target, "mteam_api", []string{"api_key"}); err != nil {
		return Report{}, err
	}
	if err := service.checkDownloader(ctx, &report, "source_downloader", input.Downloader); err != nil {
		return Report{}, err
	}
	if err := service.checkDownloader(ctx, &report, "target_downloader", input.TargetDownloader); err != nil {
		return Report{}, err
	}
	if err := service.checkImageHost(ctx, &report, input.ImageHost); err != nil {
		return Report{}, err
	}
	if err := service.checkScreenshotProfile(ctx, &report, input.ScreenshotProfile); err != nil {
		return Report{}, err
	}
	service.checkBinary(&report, "mediainfo_binary", service.runtime.MediaInfoBinary)
	service.checkBinary(&report, "ffmpeg_binary", service.runtime.FFmpegBinary)
	service.checkBinary(&report, "ffprobe_binary", service.runtime.FFprobeBinary)
	service.checkBinary(&report, "mkbrr_binary", service.runtime.MkbrrBinary)
	service.checkDownloadsDir(&report, service.runtime.DownloadsDir)

	report.ConfigurationReady = len(report.Blockers) == 0
	report.OK = report.ConfigurationReady
	if report.ConfigurationReady {
		report.Status = "configuration_ready"
		report.Summary = "本地规则、凭据字段、集成和工具配置已就绪；尚未执行外部连接、查重、下载或上传。"
		report.NextActions = append(report.NextActions,
			NextAction{Action: "explicitly_probe_integrations", Description: "在用户授权后分别探测源站、目标站和下载器；本预检本身不授权联网。"},
			NextAction{Action: "create_step_mode_retorrent_job", Description: "使用 execution_mode=step 创建任务，逐步核验实际外部证据。"},
			NextAction{Action: "collect_rule_acceptance_and_upload_confirmation", Description: "审阅不可变上传包和最终查重后，填写 obligation 证据并显式确认上传。"},
		)
	} else {
		report.Summary = fmt.Sprintf("本地 live 配置预检发现 %d 个阻塞项；未执行任何外部调用。", len(report.Blockers))
	}
	return report, nil
}

func validateInput(input Input) error {
	if !slices.Contains([]string{"U2", "CHD"}, input.Source) {
		return fmt.Errorf("%w: source must be U2 or CHD for the current reference workflow", ErrInvalid)
	}
	if input.Target != "MTEAM" {
		return fmt.Errorf("%w: target must be MTEAM for the current complete target workflow", ErrInvalid)
	}
	for name, value := range map[string]string{
		"downloader": input.Downloader, "target_downloader": input.TargetDownloader,
		"image_host": input.ImageHost, "screenshot_profile": input.ScreenshotProfile,
	} {
		if !resourceNamePattern.MatchString(value) {
			return fmt.Errorf("%w: %s must match %s", ErrInvalid, name, resourceNamePattern.String())
		}
	}
	return nil
}

func (service *Service) checkAdapter(report *Report, key, site string, supported bool, role string) {
	if supported {
		report.Checks = append(report.Checks, Check{Key: key, Status: "ready", Summary: site + " adapter is registered", Evidence: map[string]any{"site_code": site, "role": role}})
		return
	}
	report.block(key, role+"_adapter_unavailable", site+" does not have a complete callable adapter for this role", map[string]any{"site_code": site})
}

func (service *Service) checkRules(ctx context.Context, report *Report, site string) error {
	if service.rules == nil {
		return errors.New("rule readiness provider is unavailable")
	}
	revision, err := service.rules.Active(ctx, site)
	if errors.Is(err, rules.ErrNotFound) {
		report.block("rules."+site, "active_rule_required", "站点缺少已审批并激活的完整规则版本。", map[string]any{"site_code": site})
		report.NextActions = append(report.NextActions, NextAction{Action: "import_review_activate_site_rules", Description: "导入完整 Markdown、人工审批精确 fingerprint 后激活。", Parameters: map[string]any{"site_code": site}})
		return nil
	}
	if err != nil {
		return fmt.Errorf("check active rules for %s: %w", site, err)
	}
	var obligations []rules.Obligation
	if err := json.Unmarshal(revision.Obligations, &obligations); err != nil {
		return fmt.Errorf("decode active rule obligations for %s: %w", site, err)
	}
	obligationIDs := make([]string, 0, len(obligations))
	for _, obligation := range obligations {
		if obligation.Blocking {
			obligationIDs = append(obligationIDs, obligation.ID)
		}
	}
	slices.Sort(obligationIDs)
	report.Checks = append(report.Checks, Check{Key: "rules." + site, Status: "ready", Summary: "active approved rule fingerprint is available", Evidence: map[string]any{
		"site_code": site, "revision_id": revision.ID, "fingerprint": revision.Fingerprint, "blocking_obligation_ids": obligationIDs,
	}})
	report.RequiredConfirmations = append(report.RequiredConfirmations, RuleConfirmation{SiteCode: site, Fingerprint: revision.Fingerprint, ObligationIDs: obligationIDs})
	acceptRules := report.ResumeState["accept_rules"].(map[string]any)
	acceptRules[site] = map[string]any{"fingerprint": revision.Fingerprint, "accepted": false, "obligations": map[string]any{}}
	return nil
}

func (service *Service) checkSite(ctx context.Context, report *Report, site, expectedAdapter string, requiredCredentials []string) error {
	if service.integrations == nil {
		return errors.New("integration readiness provider is unavailable")
	}
	runtime, err := service.integrations.GetRuntimeSite(ctx, site)
	if isConfigurationError(err) {
		report.block("site."+site, "site_configuration_required", "站点配置不可用、已禁用或凭据无法读取。", map[string]any{"site_code": site})
		report.NextActions = append(report.NextActions, NextAction{Action: "configure_site_credentials", Description: "配置并启用站点凭据；凭据值不会由预检返回。", Parameters: map[string]any{"site_code": site, "required_fields": requiredCredentials}})
		return nil
	}
	if err != nil {
		return fmt.Errorf("check site configuration for %s: %w", site, err)
	}
	fields := presentFields(runtime.Credentials)
	missing := missingFields(fields, requiredCredentials)
	if runtime.Adapter != expectedAdapter || len(missing) > 0 {
		report.block("site."+site, "site_configuration_incomplete", "站点 adapter 或必需凭据字段不完整。", map[string]any{"site_code": site, "adapter": runtime.Adapter, "expected_adapter": expectedAdapter, "credential_fields": fields, "missing_fields": missing})
		return nil
	}
	report.Checks = append(report.Checks, Check{Key: "site." + site, Status: "ready", Summary: "site configuration and credential fields are available", Evidence: map[string]any{
		"site_code": site, "adapter": runtime.Adapter, "configuration_sha256": runtime.ConfigurationSHA256, "credential_fields": fields,
	}})
	return nil
}

func (service *Service) checkDownloader(ctx context.Context, report *Report, key, name string) error {
	runtime, err := service.integrations.GetRuntimeDownloader(ctx, name)
	if isConfigurationError(err) {
		report.block(key, "downloader_configuration_required", "下载器不存在、已禁用、凭据不完整或 adapter 无本地运行时。", map[string]any{"name": name})
		report.NextActions = append(report.NextActions, NextAction{Action: "configure_downloader", Description: "配置启用的远程下载器和路径映射，再由用户显式授权 probe。", Parameters: map[string]any{"name": name, "role": key}})
		return nil
	}
	if err != nil {
		return fmt.Errorf("check downloader %s: %w", name, err)
	}
	if !runtime.AdapterCapability.RuntimeSupported {
		report.block(key, "downloader_runtime_unavailable", "下载器 adapter 没有本地运行时支持。", map[string]any{"name": name, "adapter": runtime.Adapter})
		return nil
	}
	report.Checks = append(report.Checks, Check{Key: key, Status: "ready", Summary: "enabled downloader configuration is available", Evidence: map[string]any{
		"name": name, "adapter": runtime.Adapter, "configuration_sha256": runtime.ConfigurationSHA256,
		"credential_fields": runtime.CredentialFields, "path_mapping_count": len(runtime.PathMappings), "health_status": runtime.HealthStatus,
	}})
	return nil
}

func (service *Service) checkImageHost(ctx context.Context, report *Report, name string) error {
	runtime, err := service.integrations.GetRuntimeImageHost(ctx, name)
	if isConfigurationError(err) {
		report.block("image_host", "image_host_configuration_required", "图床不存在、已禁用或凭据无法读取。", map[string]any{"name": name})
		report.NextActions = append(report.NextActions, NextAction{Action: "configure_image_host", Description: "配置启用的 imgbb 或 PTPimg；本预检不会上传测试图片。", Parameters: map[string]any{"name": name}})
		return nil
	}
	if err != nil {
		return fmt.Errorf("check image host %s: %w", name, err)
	}
	fields := presentFields(runtime.Credentials)
	if !slices.Contains([]string{"imgbb", "ptpimg"}, runtime.Adapter) || !slices.Contains(fields, "api_key") {
		report.block("image_host", "image_host_configuration_incomplete", "图床 adapter 或 api_key 字段不完整。", map[string]any{"name": name, "adapter": runtime.Adapter, "credential_fields": fields})
		return nil
	}
	digest := sha256.Sum256(runtime.Config)
	report.Checks = append(report.Checks, Check{Key: "image_host", Status: "ready", Summary: "enabled image-host configuration is available", Evidence: map[string]any{
		"name": name, "adapter": runtime.Adapter, "configuration_sha256": hex.EncodeToString(digest[:]), "credential_fields": fields, "health_status": runtime.HealthStatus,
	}})
	return nil
}

func (service *Service) checkScreenshotProfile(ctx context.Context, report *Report, name string) error {
	profile, err := service.integrations.GetRuntimeScreenshotProfile(ctx, name)
	if isConfigurationError(err) {
		report.block("screenshot_profile", "screenshot_profile_required", "没有找到启用的截图策略版本。", map[string]any{"name": name})
		report.NextActions = append(report.NextActions, NextAction{Action: "create_screenshot_profile", Description: "创建并启用不可变截图策略版本。", Parameters: map[string]any{"name": name}})
		return nil
	}
	if err != nil {
		return fmt.Errorf("check screenshot profile %s: %w", name, err)
	}
	digest := sha256.Sum256(profile.Config)
	report.Checks = append(report.Checks, Check{Key: "screenshot_profile", Status: "ready", Summary: "enabled screenshot profile is available", Evidence: map[string]any{
		"name": profile.Name, "revision": profile.Revision, "configuration_sha256": hex.EncodeToString(digest[:]),
	}})
	return nil
}

func (service *Service) checkBinary(report *Report, key, binary string) {
	binary = strings.TrimSpace(binary)
	path, err := exec.LookPath(binary)
	if err != nil {
		report.block(key, "runtime_binary_required", "本地素材工具不可执行。", map[string]any{"binary": binary})
		report.NextActions = append(report.NextActions, NextAction{Action: "install_runtime_binary", Description: "安装或修正容器内工具路径。", Parameters: map[string]any{"binary": binary, "check": key}})
		return
	}
	report.Checks = append(report.Checks, Check{Key: key, Status: "ready", Summary: "runtime binary is executable", Evidence: map[string]any{"binary": binary, "resolved_path": path}})
}

func (service *Service) checkDownloadsDir(report *Report, path string) {
	info, err := os.Stat(path)
	if err != nil || !info.IsDir() {
		report.block("downloads_mount", "downloads_mount_required", "下载目录挂载不存在或不是目录。", map[string]any{"path": path})
		report.NextActions = append(report.NextActions, NextAction{Action: "mount_downloads_directory", Description: "把下载器内容路径映射到容器内 /downloads。", Parameters: map[string]any{"path": path}})
		return
	}
	report.Checks = append(report.Checks, Check{Key: "downloads_mount", Status: "ready", Summary: "downloads mount exists", Evidence: map[string]any{"path": path}})
}

func (report *Report) block(component, code, message string, details map[string]any) {
	report.Checks = append(report.Checks, Check{Key: component, Status: "blocked", Summary: message, Evidence: details})
	report.Blockers = append(report.Blockers, Blocker{Code: code, Message: message, Component: component, Details: details})
}

func isConfigurationError(err error) bool {
	return errors.Is(err, integrations.ErrNotFound) || errors.Is(err, integrations.ErrValidation)
}

func presentFields(values map[string]string) []string {
	fields := make([]string, 0, len(values))
	for key, value := range values {
		if strings.TrimSpace(value) != "" {
			fields = append(fields, key)
		}
	}
	slices.Sort(fields)
	return fields
}

func missingFields(present, required []string) []string {
	missing := make([]string, 0)
	for _, field := range required {
		if !slices.Contains(present, field) {
			missing = append(missing, field)
		}
	}
	return missing
}
