package rules

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
	"gopkg.in/yaml.v3"
)

const Kind = "upload-assistant.site-rule.v1"

var siteCodePattern = regexp.MustCompile(`^[A-Z0-9][A-Z0-9_-]{1,31}$`)

type Document struct {
	SchemaVersion int          `json:"schema_version" yaml:"schema_version"`
	Kind          string       `json:"kind" yaml:"kind"`
	Site          Site         `json:"site" yaml:"site"`
	Source        Source       `json:"source" yaml:"source"`
	Automation    Automation   `json:"automation" yaml:"automation"`
	Limits        Limits       `json:"limits" yaml:"limits"`
	Seeding       Seeding      `json:"seeding" yaml:"seeding"`
	Transfer      Transfer     `json:"transfer" yaml:"transfer"`
	Obligations   []Obligation `json:"obligations" yaml:"obligations"`
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
	URL        string `json:"url" yaml:"url"`
	CapturedAt string `json:"captured_at" yaml:"captured_at"`
	Complete   bool   `json:"complete" yaml:"complete"`
	Scope      string `json:"scope" yaml:"scope"`
	TextSHA256 string `json:"text_sha256,omitempty" yaml:"text_sha256,omitempty"`
}

type Automation struct {
	ManualReviewRequired bool `json:"manual_review_required" yaml:"manual_review_required"`
	Download             bool `json:"download" yaml:"download"`
	Upload               bool `json:"upload" yaml:"upload"`
	Retorrent            bool `json:"retorrent" yaml:"retorrent"`
	AutoPull             bool `json:"auto_pull" yaml:"auto_pull"`
	AutoUpload           bool `json:"auto_upload" yaml:"auto_upload"`
}

type Limits struct {
	Download string `json:"download,omitempty" yaml:"download,omitempty"`
	Upload   string `json:"upload,omitempty" yaml:"upload,omitempty"`
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

func ParseMarkdown(raw []byte) (Document, error) {
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
	if d.SchemaVersion != 1 {
		return fmt.Errorf("unsupported rule schema_version %d", d.SchemaVersion)
	}
	if d.Kind != Kind {
		return fmt.Errorf("unsupported rule kind %q", d.Kind)
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
		Limits        Limits       `json:"limits"`
		Seeding       Seeding      `json:"seeding"`
		Transfer      Transfer     `json:"transfer"`
		Obligations   []Obligation `json:"obligations"`
		Notes         []string     `json:"notes,omitempty"`
		BodySHA256    string       `json:"body_sha256"`
	}{
		SchemaVersion: d.SchemaVersion, Kind: d.Kind, Site: d.Site, Source: d.Source,
		Automation: d.Automation, Limits: d.Limits, Seeding: d.Seeding, Transfer: d.Transfer,
		Obligations: d.Obligations, Notes: d.Notes, BodySHA256: sha256Hex([]byte(d.Body)),
	}
	canonical.Source.TextSHA256 = canonical.BodySHA256
	body, err := json.Marshal(canonical)
	if err != nil {
		return "", fmt.Errorf("marshal rule fingerprint document: %w", err)
	}
	return sha256Hex(body), nil
}

func (d Document) PolicyJSON() ([]byte, error) {
	policy := struct {
		SchemaVersion int          `json:"schema_version"`
		Site          Site         `json:"site"`
		Source        Source       `json:"source"`
		Automation    Automation   `json:"automation"`
		Limits        Limits       `json:"limits"`
		Seeding       Seeding      `json:"seeding"`
		Transfer      Transfer     `json:"transfer"`
		Obligations   []Obligation `json:"obligations"`
	}{
		SchemaVersion: d.SchemaVersion, Site: d.Site, Source: d.Source,
		Automation: d.Automation, Limits: d.Limits, Seeding: d.Seeding,
		Transfer: d.Transfer, Obligations: d.Obligations,
	}
	policy.Source.TextSHA256 = sha256Hex([]byte(d.Body))
	return json.Marshal(policy)
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
