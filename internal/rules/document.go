package rules

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
	"gopkg.in/yaml.v3"
)

const (
	Kind             = "upload-assistant.site-rule.v1"
	KindV2           = "upload-assistant.site-rule.v2"
	MaxMarkdownBytes = 8 << 20
)

var siteCodePattern = regexp.MustCompile(`^[A-Z0-9][A-Z0-9_-]{1,31}$`)
var namingProfilePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)
var sha256Pattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type Document struct {
	SchemaVersion int          `json:"schema_version" yaml:"schema_version"`
	Kind          string       `json:"kind" yaml:"kind"`
	Site          Site         `json:"site" yaml:"site"`
	Source        Source       `json:"source" yaml:"source"`
	Automation    Automation   `json:"automation" yaml:"automation"`
	Access        Access       `json:"access,omitempty" yaml:"access,omitempty"`
	Limits        Limits       `json:"limits" yaml:"limits"`
	Naming        Naming       `json:"naming,omitempty" yaml:"naming,omitempty"`
	Seeding       Seeding      `json:"seeding" yaml:"seeding"`
	Transfer      Transfer     `json:"transfer" yaml:"transfer"`
	Obligations   []Obligation `json:"obligations" yaml:"obligations"`
	Advisories    []Advisory   `json:"advisories,omitempty" yaml:"advisories,omitempty"`
	Notes         []string     `json:"notes,omitempty" yaml:"notes,omitempty"`
	Review        Review       `json:"review" yaml:"review"`
	Body          string       `json:"-" yaml:"-"`
	Format        string       `json:"-" yaml:"-"`
}

type Site struct {
	Code        string   `json:"code" yaml:"code"`
	DisplayName string   `json:"display_name" yaml:"display_name"`
	Roles       []string `json:"roles" yaml:"roles"`
}

type Source struct {
	URL        string           `json:"url" yaml:"url"`
	CapturedAt string           `json:"captured_at" yaml:"captured_at"`
	Complete   bool             `json:"complete" yaml:"complete"`
	Scope      string           `json:"scope" yaml:"scope"`
	TextSHA256 string           `json:"text_sha256,omitempty" yaml:"text_sha256,omitempty"`
	Documents  []SourceDocument `json:"documents,omitempty" yaml:"documents,omitempty"`
	Conflicts  []SourceConflict `json:"conflicts,omitempty" yaml:"conflicts,omitempty"`
}

// SourceDocument preserves immutable per-page provenance when one rule
// revision was compiled from several independently captured pages. The body
// remains the checksum-bound normalized source text; raw authenticated HTML is
// never persisted in the rule document.
type SourceDocument struct {
	ID          string `json:"id" yaml:"id"`
	URL         string `json:"url" yaml:"url"`
	Scope       string `json:"scope" yaml:"scope"`
	AuthMode    string `json:"auth_mode,omitempty" yaml:"auth_mode,omitempty"`
	CapturedAt  string `json:"captured_at" yaml:"captured_at"`
	TextSHA256  string `json:"text_sha256" yaml:"text_sha256"`
	ContentType string `json:"content_type,omitempty" yaml:"content_type,omitempty"`
	SizeBytes   int64  `json:"size_bytes,omitempty" yaml:"size_bytes,omitempty"`
}

// SourceConflict is an unresolved contradiction between source pages. A
// draft containing one cannot be approved until an operator derives a
// corrected immutable revision.
type SourceConflict struct {
	Section      string   `json:"section" yaml:"section"`
	Summary      string   `json:"summary" yaml:"summary"`
	EvidenceRefs []string `json:"evidence_refs" yaml:"evidence_refs"`
}

type Automation struct {
	ManualReviewRequired bool `json:"manual_review_required" yaml:"manual_review_required"`
	Download             bool `json:"download" yaml:"download"`
	Upload               bool `json:"upload" yaml:"upload"`
	Retorrent            bool `json:"retorrent" yaml:"retorrent"`
	AutoPull             bool `json:"auto_pull" yaml:"auto_pull"`
	AutoUpload           bool `json:"auto_upload" yaml:"auto_upload"`
}

// Access is the human-reviewed tracker network policy. Version 1 documents do
// not contain this section and therefore never authorize network access.
// Numeric values are optional upper bounds from the tracker rule; the
// operator policy is always required and the stricter value wins.
type Access struct {
	ServiceAccess             string `json:"service_access,omitempty" yaml:"service_access,omitempty"`
	SearchAccess              string `json:"search_access,omitempty" yaml:"search_access,omitempty"`
	GeneralMinIntervalSeconds int    `json:"general_min_interval_seconds,omitempty" yaml:"general_min_interval_seconds,omitempty"`
	GeneralMaxRequestsPerHour int    `json:"general_max_requests_per_hour,omitempty" yaml:"general_max_requests_per_hour,omitempty"`
	SearchMinIntervalSeconds  int    `json:"search_min_interval_seconds,omitempty" yaml:"search_min_interval_seconds,omitempty"`
	SearchMaxRequestsPerHour  int    `json:"search_max_requests_per_hour,omitempty" yaml:"search_max_requests_per_hour,omitempty"`
	MaxConcurrency            int    `json:"max_concurrency,omitempty" yaml:"max_concurrency,omitempty"`
}

type Limits struct {
	Download            string           `json:"download,omitempty" yaml:"download,omitempty"`
	Upload              string           `json:"upload,omitempty" yaml:"upload,omitempty"`
	SeedboxUpload       string           `json:"seedbox_upload,omitempty" yaml:"seedbox_upload,omitempty"`
	DownloadPolicy      *RateLimitPolicy `json:"download_policy,omitempty" yaml:"download_policy,omitempty"`
	UploadPolicy        *RateLimitPolicy `json:"upload_policy,omitempty" yaml:"upload_policy,omitempty"`
	SeedboxUploadPolicy *RateLimitPolicy `json:"seedbox_upload_policy,omitempty" yaml:"seedbox_upload_policy,omitempty"`
}

// RateLimitPolicy separates the tracker-declared value from the conservative
// per-torrent value actually sent to a downloader. Legacy scalar limit fields
// remain the executable value for compatibility with existing workers.
type RateLimitPolicy struct {
	Declared     string   `json:"declared,omitempty" yaml:"declared,omitempty"`
	SafetyMargin string   `json:"safety_margin,omitempty" yaml:"safety_margin,omitempty"`
	Enforced     string   `json:"enforced,omitempty" yaml:"enforced,omitempty"`
	Scope        string   `json:"scope" yaml:"scope"`
	EvidenceRefs []string `json:"evidence_refs,omitempty" yaml:"evidence_refs,omitempty"`
}

// Naming contains the deterministic naming gates that can be checked before a
// target package is accepted. AI may propose these fields, but a human must
// review them before the immutable revision can be approved.
type Naming struct {
	ReleaseTitle NamingConstraint `json:"release_title,omitempty" yaml:"release_title,omitempty"`
	ContentName  NamingConstraint `json:"content_name,omitempty" yaml:"content_name,omitempty"`
	Profiles     []NamingProfile  `json:"profiles,omitempty" yaml:"profiles,omitempty"`
}

// NamingProfile selects one deterministic release-title grammar for a
// resource class. The operator or adapter must select the profile before the
// target package can pass its naming gate; a profile is never guessed from a
// tracker category name.
type NamingProfile struct {
	ID              string           `json:"id" yaml:"id"`
	Label           string           `json:"label" yaml:"label"`
	ResourceClasses []string         `json:"resource_classes,omitempty" yaml:"resource_classes,omitempty"`
	CategoryIDs     []int            `json:"category_ids,omitempty" yaml:"category_ids,omitempty"`
	TitleTokens     []NamingToken    `json:"title_tokens,omitempty" yaml:"title_tokens,omitempty"`
	ReleaseTitle    NamingConstraint `json:"release_title" yaml:"release_title"`
}

// NamingToken is an ordered deterministic title component. Token values come
// only from verified metadata, media evidence, or a parsed source title;
// required missing values block packaging instead of being invented.
type NamingToken struct {
	Kind      string `json:"kind" yaml:"kind"`
	Value     string `json:"value" yaml:"value"`
	Required  bool   `json:"required,omitempty" yaml:"required,omitempty"`
	Separator string `json:"separator,omitempty" yaml:"separator,omitempty"`
}

type NamingConstraint struct {
	Required     bool     `json:"required" yaml:"required"`
	Pattern      string   `json:"pattern,omitempty" yaml:"pattern,omitempty"`
	Template     string   `json:"template,omitempty" yaml:"template,omitempty"`
	MaxLength    int      `json:"max_length,omitempty" yaml:"max_length,omitempty"`
	EvidenceRefs []string `json:"evidence_refs,omitempty" yaml:"evidence_refs,omitempty"`
}

// Advisory is structured preflight guidance. It is visible before upload but
// is not an approval check and cannot waive any core workflow safety gate.
type Advisory struct {
	Section      string   `json:"section" yaml:"section"`
	Severity     string   `json:"severity" yaml:"severity"`
	Summary      string   `json:"summary" yaml:"summary"`
	EvidenceRefs []string `json:"evidence_refs,omitempty" yaml:"evidence_refs,omitempty"`
}

type Seeding struct {
	MinimumTimeHours int     `json:"minimum_time_hours,omitempty" yaml:"minimum_time_hours,omitempty"`
	MinimumRatio     float64 `json:"minimum_ratio,omitempty" yaml:"minimum_ratio,omitempty"`
}

type Transfer struct {
	FreeleechRequired      bool     `json:"freeleech_required" yaml:"freeleech_required"`
	ForbidOriginalTorrent  bool     `json:"forbid_original_torrent" yaml:"forbid_original_torrent"`
	PreserveContent        bool     `json:"preserve_content" yaml:"preserve_content"`
	RequiredPromotions     []string `json:"required_promotions,omitempty" yaml:"required_promotions,omitempty"`
	ForbiddenTitlePatterns []string `json:"forbidden_title_patterns,omitempty" yaml:"forbidden_title_patterns,omitempty"`
	ForbiddenReleaseGroups []string `json:"forbidden_release_groups,omitempty" yaml:"forbidden_release_groups,omitempty"`
}

type Obligation struct {
	ID           string   `json:"id" yaml:"id" toml:"id"`
	Scope        string   `json:"scope" yaml:"scope" toml:"scope"`
	Verification string   `json:"verification" yaml:"verification" toml:"verification"`
	Blocking     bool     `json:"blocking" yaml:"blocking" toml:"blocking"`
	Resolution   string   `json:"resolution" yaml:"resolution" toml:"resolution"`
	Description  string   `json:"description" yaml:"description" toml:"description"`
	EvidenceRefs []string `json:"evidence_refs,omitempty" yaml:"evidence_refs,omitempty" toml:"evidence_refs"`
	Enforcement  string   `json:"enforcement" yaml:"enforcement" toml:"enforcement"`
}

type Review struct {
	Status      string `json:"status" yaml:"status"`
	Reviewer    string `json:"reviewer,omitempty" yaml:"reviewer,omitempty"`
	ReviewedAt  string `json:"reviewed_at,omitempty" yaml:"reviewed_at,omitempty"`
	Fingerprint string `json:"fingerprint,omitempty" yaml:"fingerprint,omitempty"`
}

// Policy is the executable, immutable portion of a rule document. Review
// metadata and the original Markdown body are deliberately excluded.
type Policy struct {
	SchemaVersion int          `json:"schema_version"`
	Site          Site         `json:"site"`
	Source        Source       `json:"source"`
	Automation    Automation   `json:"automation"`
	Access        Access       `json:"access,omitempty"`
	Limits        Limits       `json:"limits"`
	Naming        Naming       `json:"naming,omitempty"`
	Seeding       Seeding      `json:"seeding"`
	Transfer      Transfer     `json:"transfer"`
	Obligations   []Obligation `json:"obligations"`
	Advisories    []Advisory   `json:"advisories,omitempty"`
}

func ParseMarkdown(raw []byte) (Document, error) {
	if len(raw) > MaxMarkdownBytes {
		return Document{}, fmt.Errorf("rule Markdown exceeds %d bytes", MaxMarkdownBytes)
	}
	normalized := bytes.ReplaceAll(raw, []byte("\r\n"), []byte("\n"))
	frontMatter, body, format, err := splitFrontMatter(normalized)
	if err != nil {
		return Document{}, err
	}
	var document Document
	switch format {
	case "yaml":
		decoder := yaml.NewDecoder(bytes.NewReader(frontMatter))
		decoder.KnownFields(true)
		if err := decoder.Decode(&document); err != nil {
			return Document{}, fmt.Errorf("decode YAML rule front matter: %w", err)
		}
	case "toml":
		legacy, err := parseLegacyTOML(frontMatter)
		if err != nil {
			return Document{}, err
		}
		document = legacy
	default:
		return Document{}, fmt.Errorf("unsupported rule front matter format %q", format)
	}
	document.Body = strings.TrimSpace(string(body))
	document.Format = format
	document.Site.Code = strings.ToUpper(strings.TrimSpace(document.Site.Code))
	if document.Kind == "ptcli.site_rule_document.v1" {
		document.Kind = Kind
	}
	if document.Review.Status == "" {
		document.Review.Status = "draft"
	}
	if err := document.Validate(); err != nil {
		return Document{}, err
	}
	return document, nil
}

func (d Document) Validate() error {
	if d.SchemaVersion != 1 && d.SchemaVersion != 2 {
		return fmt.Errorf("unsupported rule schema_version %d", d.SchemaVersion)
	}
	if (d.SchemaVersion == 1 && d.Kind != Kind) || (d.SchemaVersion == 2 && d.Kind != KindV2) {
		return fmt.Errorf("unsupported rule kind %q", d.Kind)
	}
	if err := validateAccess(d.SchemaVersion, d.Access); err != nil {
		return err
	}
	d.Site.Code = strings.ToUpper(strings.TrimSpace(d.Site.Code))
	if !siteCodePattern.MatchString(d.Site.Code) {
		return fmt.Errorf("invalid site code %q", d.Site.Code)
	}
	if strings.TrimSpace(d.Site.DisplayName) == "" {
		return fmt.Errorf("site display_name is required")
	}
	if len(d.Site.Roles) == 0 {
		return fmt.Errorf("at least one site role is required")
	}
	for _, role := range d.Site.Roles {
		if role != "source" && role != "target" {
			return fmt.Errorf("invalid site role %q", role)
		}
	}
	if d.Source.URL == "" {
		return fmt.Errorf("rule source URL is required")
	}
	parsedURL, err := url.Parse(d.Source.URL)
	if err != nil || parsedURL.Scheme != "https" || parsedURL.Host == "" {
		return fmt.Errorf("rule source URL must be an absolute HTTPS URL")
	}
	if _, err := time.Parse("2006-01-02", d.Source.CapturedAt); err != nil {
		if _, timeErr := time.Parse(time.RFC3339, d.Source.CapturedAt); timeErr != nil {
			return fmt.Errorf("source captured_at must be a date or RFC3339 timestamp")
		}
	}
	if d.Source.Scope == "" || d.Body == "" {
		return fmt.Errorf("source scope and original rule body are required")
	}
	if err := validateSourceMetadata(d.Source); err != nil {
		return err
	}
	if err := validateSeeding(d.Seeding); err != nil {
		return err
	}
	if err := validateLimits(d.Limits); err != nil {
		return err
	}
	if err := validateNaming(d.Naming); err != nil {
		return err
	}
	if err := validateAdvisories(d.Advisories); err != nil {
		return err
	}
	bodyHash := sha256Hex([]byte(d.Body))
	if d.Source.TextSHA256 != "" && !strings.EqualFold(d.Source.TextSHA256, bodyHash) {
		return fmt.Errorf("source text SHA-256 does not match the Markdown body")
	}
	seen := make(map[string]struct{}, len(d.Obligations))
	for _, obligation := range d.Obligations {
		if obligation.ID == "" || obligation.Scope == "" || obligation.Description == "" || obligation.Enforcement == "" {
			return fmt.Errorf("obligation id, scope, description, and enforcement are required")
		}
		if _, exists := seen[obligation.ID]; exists {
			return fmt.Errorf("duplicate obligation %q", obligation.ID)
		}
		seen[obligation.ID] = struct{}{}
		if obligation.Verification != "manual" && obligation.Verification != "programmatic" {
			return fmt.Errorf("obligation %s has invalid verification %q", obligation.ID, obligation.Verification)
		}
		switch obligation.Resolution {
		case "pending", "enforced", "not_applicable":
		default:
			return fmt.Errorf("obligation %s has invalid resolution %q", obligation.ID, obligation.Resolution)
		}
	}
	switch d.Review.Status {
	case "draft", "approved", "retired":
	default:
		return fmt.Errorf("invalid review status %q", d.Review.Status)
	}
	return nil
}

func (d Document) Fingerprint() (string, error) {
	canonical := struct {
		SchemaVersion int          `json:"schema_version"`
		Kind          string       `json:"kind"`
		Site          Site         `json:"site"`
		Source        Source       `json:"source"`
		Automation    Automation   `json:"automation"`
		Access        Access       `json:"access,omitempty"`
		Limits        Limits       `json:"limits"`
		Naming        Naming       `json:"naming,omitempty"`
		Seeding       Seeding      `json:"seeding"`
		Transfer      Transfer     `json:"transfer"`
		Obligations   []Obligation `json:"obligations"`
		Advisories    []Advisory   `json:"advisories,omitempty"`
		Notes         []string     `json:"notes,omitempty"`
		BodySHA256    string       `json:"body_sha256"`
	}{
		SchemaVersion: d.SchemaVersion, Kind: d.Kind, Site: d.Site, Source: d.Source,
		Automation: d.Automation, Access: d.Access, Limits: d.Limits, Naming: d.Naming, Seeding: d.Seeding, Transfer: d.Transfer,
		Obligations: d.Obligations, Advisories: d.Advisories, Notes: d.Notes, BodySHA256: sha256Hex([]byte(d.Body)),
	}
	canonical.Source.TextSHA256 = canonical.BodySHA256
	body, err := json.Marshal(canonical)
	if err != nil {
		return "", fmt.Errorf("marshal rule fingerprint document: %w", err)
	}
	return sha256Hex(body), nil
}

func (d Document) PolicyJSON() ([]byte, error) {
	policy := Policy{
		SchemaVersion: d.SchemaVersion, Site: d.Site, Source: d.Source,
		Automation: d.Automation, Access: d.Access, Limits: d.Limits, Naming: d.Naming, Seeding: d.Seeding,
		Transfer: d.Transfer, Obligations: d.Obligations, Advisories: d.Advisories,
	}
	policy.Source.TextSHA256 = sha256Hex([]byte(d.Body))
	return json.Marshal(policy)
}

func ParsePolicy(raw []byte) (Policy, error) {
	var policy Policy
	if err := json.Unmarshal(raw, &policy); err != nil {
		return Policy{}, fmt.Errorf("decode executable rule policy: %w", err)
	}
	if (policy.SchemaVersion != 1 && policy.SchemaVersion != 2) || !siteCodePattern.MatchString(policy.Site.Code) {
		return Policy{}, fmt.Errorf("invalid executable rule policy")
	}
	if err := validateAccess(policy.SchemaVersion, policy.Access); err != nil {
		return Policy{}, err
	}
	if err := validateSeeding(policy.Seeding); err != nil {
		return Policy{}, err
	}
	if err := validateLimits(policy.Limits); err != nil {
		return Policy{}, err
	}
	if err := validateNaming(policy.Naming); err != nil {
		return Policy{}, err
	}
	if err := validateAdvisories(policy.Advisories); err != nil {
		return Policy{}, err
	}
	return policy, nil
}

func validateLimits(limits Limits) error {
	for name, value := range map[string]string{
		"download": limits.Download, "upload": limits.Upload, "seedbox_upload": limits.SeedboxUpload,
	} {
		if _, err := ParseByteRate(value); err != nil {
			return fmt.Errorf("limits.%s is invalid: %w", name, err)
		}
	}
	for name, policy := range map[string]*RateLimitPolicy{
		"download_policy": limits.DownloadPolicy, "upload_policy": limits.UploadPolicy,
		"seedbox_upload_policy": limits.SeedboxUploadPolicy,
	} {
		if policy == nil {
			continue
		}
		if err := validateRateLimitPolicy("limits."+name, *policy); err != nil {
			return err
		}
	}
	if limits.DownloadPolicy != nil && strings.TrimSpace(limits.DownloadPolicy.Enforced) != strings.TrimSpace(limits.Download) {
		return fmt.Errorf("limits.download must equal limits.download_policy.enforced")
	}
	if limits.UploadPolicy != nil && strings.TrimSpace(limits.UploadPolicy.Enforced) != strings.TrimSpace(limits.Upload) {
		return fmt.Errorf("limits.upload must equal limits.upload_policy.enforced")
	}
	if limits.SeedboxUploadPolicy != nil && strings.TrimSpace(limits.SeedboxUploadPolicy.Enforced) != strings.TrimSpace(limits.SeedboxUpload) {
		return fmt.Errorf("limits.seedbox_upload must equal limits.seedbox_upload_policy.enforced")
	}
	return nil
}

func validateRateLimitPolicy(path string, policy RateLimitPolicy) error {
	if policy.Scope != "per_torrent" && policy.Scope != "account_total" && policy.Scope != "site_total" && policy.Scope != "unknown" {
		return fmt.Errorf("%s.scope must be per_torrent, account_total, site_total, or unknown", path)
	}
	declared, err := ParseByteRate(policy.Declared)
	if err != nil {
		return fmt.Errorf("%s.declared is invalid: %w", path, err)
	}
	margin, err := ParseByteRate(policy.SafetyMargin)
	if err != nil {
		return fmt.Errorf("%s.safety_margin is invalid: %w", path, err)
	}
	enforced, err := ParseByteRate(policy.Enforced)
	if err != nil {
		return fmt.Errorf("%s.enforced is invalid: %w", path, err)
	}
	if enforced > 0 && declared == 0 {
		return fmt.Errorf("%s.declared is required when an enforced value is set", path)
	}
	if declared > 0 && enforced > declared {
		return fmt.Errorf("%s.enforced must not exceed the declared value", path)
	}
	if margin > declared && declared > 0 {
		return fmt.Errorf("%s.safety_margin must not exceed the declared value", path)
	}
	if policy.Scope != "per_torrent" && enforced > 0 {
		return fmt.Errorf("%s cannot enforce a non-per-torrent limit", path)
	}
	if len(policy.EvidenceRefs) > 32 {
		return fmt.Errorf("%s.evidence_refs exceeds 32 items", path)
	}
	for _, reference := range policy.EvidenceRefs {
		if strings.TrimSpace(reference) == "" || len(reference) > 500 {
			return fmt.Errorf("%s contains an invalid evidence reference", path)
		}
	}
	return nil
}

func validateSourceMetadata(source Source) error {
	if len(source.Documents) > 20 {
		return fmt.Errorf("source.documents exceeds 20 items")
	}
	seen := map[string]struct{}{}
	for index, document := range source.Documents {
		if !namingProfilePattern.MatchString(strings.TrimSpace(document.ID)) {
			return fmt.Errorf("source.documents[%d].id is invalid", index)
		}
		if _, exists := seen[document.ID]; exists {
			return fmt.Errorf("duplicate source document %q", document.ID)
		}
		seen[document.ID] = struct{}{}
		parsed, err := url.Parse(strings.TrimSpace(document.URL))
		if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
			return fmt.Errorf("source.documents[%d].url must be an absolute credential-free HTTPS URL", index)
		}
		if strings.TrimSpace(document.Scope) == "" || len(document.Scope) > 500 {
			return fmt.Errorf("source.documents[%d].scope is required and bounded", index)
		}
		if document.AuthMode != "" && document.AuthMode != "none" && document.AuthMode != "site_cookie" {
			return fmt.Errorf("source.documents[%d].auth_mode is invalid", index)
		}
		if _, err := time.Parse(time.RFC3339, document.CapturedAt); err != nil {
			return fmt.Errorf("source.documents[%d].captured_at must be RFC3339", index)
		}
		if !sha256Pattern.MatchString(strings.ToLower(document.TextSHA256)) {
			return fmt.Errorf("source.documents[%d].text_sha256 is invalid", index)
		}
		if document.SizeBytes < 1 || document.SizeBytes > MaxMarkdownBytes {
			return fmt.Errorf("source.documents[%d].size_bytes is invalid", index)
		}
	}
	if len(source.Conflicts) > 32 {
		return fmt.Errorf("source.conflicts exceeds 32 items")
	}
	for index, conflict := range source.Conflicts {
		if strings.TrimSpace(conflict.Section) == "" || strings.TrimSpace(conflict.Summary) == "" || len(conflict.EvidenceRefs) < 2 {
			return fmt.Errorf("source.conflicts[%d] requires section, summary, and at least two evidence refs", index)
		}
		if len(conflict.Section) > 64 || len(conflict.Summary) > 2000 || len(conflict.EvidenceRefs) > 32 {
			return fmt.Errorf("source.conflicts[%d] exceeds bounded size", index)
		}
	}
	return nil
}

func validateNaming(naming Naming) error {
	for name, constraint := range map[string]NamingConstraint{
		"release_title": naming.ReleaseTitle,
		"content_name":  naming.ContentName,
	} {
		if err := validateNamingConstraint("naming."+name, constraint); err != nil {
			return err
		}
	}
	if len(naming.Profiles) > 32 {
		return fmt.Errorf("naming.profiles exceeds 32 items")
	}
	seen := make(map[string]struct{}, len(naming.Profiles))
	for index, profile := range naming.Profiles {
		profile.ID = strings.TrimSpace(profile.ID)
		profile.Label = strings.TrimSpace(profile.Label)
		if !namingProfilePattern.MatchString(profile.ID) {
			return fmt.Errorf("naming.profiles[%d].id is invalid", index)
		}
		if _, exists := seen[profile.ID]; exists {
			return fmt.Errorf("duplicate naming profile %q", profile.ID)
		}
		seen[profile.ID] = struct{}{}
		if profile.Label == "" || len(profile.Label) > 128 {
			return fmt.Errorf("naming.profiles[%d].label is required and must not exceed 128 bytes", index)
		}
		if !profile.ReleaseTitle.Required {
			return fmt.Errorf("naming.profiles[%d].release_title.required must be true", index)
		}
		if err := validateNamingConstraint(fmt.Sprintf("naming.profiles[%d].release_title", index), profile.ReleaseTitle); err != nil {
			return err
		}
		if len(profile.ResourceClasses) > 32 || len(profile.CategoryIDs) > 128 || len(profile.TitleTokens) > 64 {
			return fmt.Errorf("naming.profiles[%d] selectors or tokens exceed bounded size", index)
		}
		for _, class := range profile.ResourceClasses {
			if !namingProfilePattern.MatchString(class) {
				return fmt.Errorf("naming.profiles[%d] contains invalid resource class %q", index, class)
			}
		}
		for _, categoryID := range profile.CategoryIDs {
			if categoryID < 1 || categoryID > 100000 {
				return fmt.Errorf("naming.profiles[%d] contains invalid category id", index)
			}
		}
		for tokenIndex, token := range profile.TitleTokens {
			if err := validateNamingToken(token); err != nil {
				return fmt.Errorf("naming.profiles[%d].title_tokens[%d]: %w", index, tokenIndex, err)
			}
		}
	}
	return nil
}

func validateNamingToken(token NamingToken) error {
	if token.Kind != "field" && token.Kind != "literal" {
		return fmt.Errorf("kind must be field or literal")
	}
	if token.Kind == "field" {
		valid := map[string]bool{
			"title": true, "year": true, "season_episode": true, "resolution": true,
			"source": true, "release_type": true, "video_codec": true, "audio_codec": true,
			"audio_channels": true, "hdr": true, "language": true, "edition": true, "group": true,
		}
		if !valid[token.Value] {
			return fmt.Errorf("unsupported field token %q", token.Value)
		}
	} else if strings.TrimSpace(token.Value) == "" || len(token.Value) > 64 {
		return fmt.Errorf("literal value is required and must not exceed 64 bytes")
	}
	if len(token.Separator) > 8 {
		return fmt.Errorf("separator exceeds 8 bytes")
	}
	return nil
}

func validateNamingConstraint(path string, constraint NamingConstraint) error {
	constraint.Pattern = strings.TrimSpace(constraint.Pattern)
	constraint.Template = strings.TrimSpace(constraint.Template)
	if constraint.MaxLength < 0 || constraint.MaxLength > 4096 {
		return fmt.Errorf("%s.max_length must be between 0 and 4096", path)
	}
	if len(constraint.Pattern) > 4096 || len(constraint.Template) > 4096 {
		return fmt.Errorf("%s pattern or template exceeds 4096 bytes", path)
	}
	if constraint.Required && constraint.Pattern == "" {
		return fmt.Errorf("%s.pattern is required for an enforceable naming gate", path)
	}
	if constraint.Pattern != "" {
		if !strings.HasPrefix(constraint.Pattern, "^") || !strings.HasSuffix(constraint.Pattern, "$") {
			return fmt.Errorf("%s.pattern must be anchored with ^ and $", path)
		}
		if _, err := regexp.Compile(constraint.Pattern); err != nil {
			return fmt.Errorf("%s.pattern is invalid: %w", path, err)
		}
	}
	if len(constraint.EvidenceRefs) > 32 {
		return fmt.Errorf("%s.evidence_refs exceeds 32 items", path)
	}
	for _, reference := range constraint.EvidenceRefs {
		if strings.TrimSpace(reference) == "" || len(reference) > 500 {
			return fmt.Errorf("%s contains an invalid evidence reference", path)
		}
	}
	return nil
}

func validateAdvisories(advisories []Advisory) error {
	if len(advisories) > 100 {
		return fmt.Errorf("rule advisories exceed 100 items")
	}
	for index, advisory := range advisories {
		if strings.TrimSpace(advisory.Section) == "" || strings.TrimSpace(advisory.Summary) == "" {
			return fmt.Errorf("advisory %d section and summary are required", index+1)
		}
		if advisory.Severity != "info" && advisory.Severity != "warning" {
			return fmt.Errorf("advisory %d severity must be info or warning", index+1)
		}
		if len(advisory.Section) > 64 || len(advisory.Summary) > 2000 || len(advisory.EvidenceRefs) > 32 {
			return fmt.Errorf("advisory %d exceeds the bounded configuration size", index+1)
		}
		for _, reference := range advisory.EvidenceRefs {
			if strings.TrimSpace(reference) == "" || len(reference) > 500 {
				return fmt.Errorf("advisory %d contains an invalid evidence reference", index+1)
			}
		}
	}
	return nil
}

func validateAccess(schemaVersion int, access Access) error {
	if schemaVersion == 1 {
		if access != (Access{}) {
			return fmt.Errorf("site-rule v1 cannot contain an access policy")
		}
		return nil
	}
	validMode := func(value string) bool {
		return value == "allowed" || value == "forbidden" || value == "undetermined"
	}
	if !validMode(access.ServiceAccess) || !validMode(access.SearchAccess) {
		return fmt.Errorf("site-rule v2 access modes must be allowed, forbidden, or undetermined")
	}
	for name, value := range map[string]int{
		"general_min_interval_seconds": access.GeneralMinIntervalSeconds,
		"search_min_interval_seconds":  access.SearchMinIntervalSeconds,
	} {
		if value < 0 || value > 86400 {
			return fmt.Errorf("access.%s must be between 0 and 86400", name)
		}
	}
	for name, value := range map[string]int{
		"general_max_requests_per_hour": access.GeneralMaxRequestsPerHour,
		"search_max_requests_per_hour":  access.SearchMaxRequestsPerHour,
	} {
		if value < 0 || value > 3600 {
			return fmt.Errorf("access.%s must be between 0 and 3600", name)
		}
	}
	if access.MaxConcurrency < 0 || access.MaxConcurrency > 4 {
		return fmt.Errorf("access.max_concurrency must be between 0 and 4")
	}
	return nil
}

func validateSeeding(seeding Seeding) error {
	if seeding.MinimumTimeHours < 0 || seeding.MinimumTimeHours > 10*365*24 {
		return fmt.Errorf("minimum seeding time must be between 0 and 87600 hours")
	}
	if math.IsNaN(seeding.MinimumRatio) || math.IsInf(seeding.MinimumRatio, 0) || seeding.MinimumRatio < 0 || seeding.MinimumRatio > 1_000_000 {
		return fmt.Errorf("minimum seeding ratio must be between 0 and 1000000")
	}
	return nil
}

func splitFrontMatter(raw []byte) ([]byte, []byte, string, error) {
	lines := bytes.Split(raw, []byte("\n"))
	if len(lines) < 3 {
		return nil, nil, "", fmt.Errorf("rule Markdown is missing front matter")
	}
	marker := string(bytes.TrimSpace(lines[0]))
	format := ""
	switch marker {
	case "---":
		format = "yaml"
	case "+++":
		format = "toml"
	default:
		return nil, nil, "", fmt.Errorf("rule Markdown must start with --- or +++ front matter")
	}
	for index := 1; index < len(lines); index++ {
		if string(bytes.TrimSpace(lines[index])) == marker {
			return bytes.Join(lines[1:index], []byte("\n")), bytes.Join(lines[index+1:], []byte("\n")), format, nil
		}
	}
	return nil, nil, "", fmt.Errorf("rule Markdown front matter is not closed")
}

type legacyDocument struct {
	SchemaVersion     int      `toml:"schema_version"`
	Kind              string   `toml:"kind"`
	Tracker           string   `toml:"tracker"`
	DisplayName       string   `toml:"display_name"`
	Roles             []string `toml:"roles"`
	RulesURL          string   `toml:"rules_url"`
	CapturedAt        string   `toml:"captured_at"`
	SourceComplete    bool     `toml:"source_complete"`
	SourceScope       string   `toml:"source_scope"`
	SourceTextSHA256  string   `toml:"source_text_sha256"`
	ReviewStatus      string   `toml:"review_status"`
	Reviewer          string   `toml:"reviewer"`
	ReviewedAt        string   `toml:"reviewed_at"`
	ReviewFingerprint string   `toml:"review_fingerprint"`
	Notes             []string `toml:"notes"`
	Automation        struct {
		ManualReviewRequired bool `toml:"manual_review_required"`
		Download             bool `toml:"download"`
		Upload               bool `toml:"upload"`
		Retorrent            bool `toml:"retorrent"`
	} `toml:"automation"`
	QBitLimits struct {
		Download string `toml:"download_limit"`
		Upload   string `toml:"upload_limit"`
	} `toml:"qbit_limits"`
	SeedingRequirements struct {
		MinimumTimeHours int     `toml:"min_seed_time_hours"`
		MinimumRatio     float64 `toml:"min_ratio"`
	} `toml:"seeding_requirements"`
	TransferRules struct {
		FreeleechRequired      bool     `toml:"freeleech_required"`
		RequiredPromotions     []string `toml:"required_promotions"`
		ForbiddenTitlePatterns []string `toml:"forbidden_title_patterns"`
		ForbiddenReleaseGroups []string `toml:"forbidden_release_groups"`
	} `toml:"transfer_rules"`
	Obligations []Obligation `toml:"obligations"`
}

func parseLegacyTOML(frontMatter []byte) (Document, error) {
	var legacy legacyDocument
	if _, err := toml.Decode(string(frontMatter), &legacy); err != nil {
		return Document{}, fmt.Errorf("decode legacy TOML rule front matter: %w", err)
	}
	return Document{
		SchemaVersion: legacy.SchemaVersion,
		Kind:          legacy.Kind,
		Site:          Site{Code: legacy.Tracker, DisplayName: legacy.DisplayName, Roles: legacy.Roles},
		Source: Source{
			URL: legacy.RulesURL, CapturedAt: legacy.CapturedAt, Complete: legacy.SourceComplete,
			Scope: legacy.SourceScope, TextSHA256: legacy.SourceTextSHA256,
		},
		Automation: Automation{
			ManualReviewRequired: legacy.Automation.ManualReviewRequired,
			Download:             legacy.Automation.Download, Upload: legacy.Automation.Upload,
			Retorrent: legacy.Automation.Retorrent,
		},
		Limits: Limits{Download: legacy.QBitLimits.Download, Upload: legacy.QBitLimits.Upload},
		Seeding: Seeding{
			MinimumTimeHours: legacy.SeedingRequirements.MinimumTimeHours,
			MinimumRatio:     legacy.SeedingRequirements.MinimumRatio,
		},
		Transfer: Transfer{
			FreeleechRequired:      legacy.TransferRules.FreeleechRequired,
			RequiredPromotions:     legacy.TransferRules.RequiredPromotions,
			ForbiddenTitlePatterns: legacy.TransferRules.ForbiddenTitlePatterns,
			ForbiddenReleaseGroups: legacy.TransferRules.ForbiddenReleaseGroups,
		},
		Obligations: legacy.Obligations,
		Notes:       legacy.Notes,
		Review: Review{
			Status: legacy.ReviewStatus, Reviewer: legacy.Reviewer,
			ReviewedAt: legacy.ReviewedAt, Fingerprint: legacy.ReviewFingerprint,
		},
	}, nil
}

func sha256Hex(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}
