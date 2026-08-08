package mteam

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/sites"
)

const (
	maxNameRunes        = 255
	maxDescriptionRunes = 1_000_000
)

type PackageAdapter struct{}

func NewPackageAdapter() PackageAdapter { return PackageAdapter{} }

func (PackageAdapter) SiteCode() string { return "MTEAM" }

type packageOptions struct {
	Name             string `json:"name,omitempty"`
	SmallDescription string `json:"small_descr,omitempty"`
	Category         int    `json:"category,omitempty"`
	CategoryEvidence string `json:"category_evidence,omitempty"`
	Standard         int    `json:"standard,omitempty"`
	Anonymous        *bool  `json:"anonymous,omitempty"`
}

func (PackageAdapter) PreparePackage(_ context.Context, material sites.TargetPackageMaterial) (sites.PreparedTargetPackage, error) {
	if material.Target != "MTEAM" {
		return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_adapter_mismatch", "MTEAM package adapter received another target site", false, nil)
	}
	options, err := decodeOptions(material.Options)
	if err != nil {
		return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", err.Error(), false, err)
	}
	title := cleanField(firstNonEmpty(options.Name, material.Title, material.Source.Name))
	smallDescription := cleanField(firstNonEmpty(options.SmallDescription, material.Title, material.Source.Name))
	requirements := make([]sites.PackageRequirement, 0, 3)
	if title == "" {
		requirements = append(requirements, requirement("target_package.name", "target_name_required", "MTEAM requires a non-empty release name"))
	} else if utf8.RuneCountInString(title) > maxNameRunes {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_name_too_long", Field: "target_package.name",
			Message:    "MTEAM release name exceeds the reviewed 255-character limit; provide an explicit shorter name",
			Parameters: map[string]any{"current_length": utf8.RuneCountInString(title), "maximum": maxNameRunes},
		})
	}
	if smallDescription == "" {
		requirements = append(requirements, requirement("target_package.small_descr", "target_small_description_required", "MTEAM requires a non-empty short description"))
	} else if utf8.RuneCountInString(smallDescription) > maxNameRunes {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_small_description_too_long", Field: "target_package.small_descr",
			Message:    "MTEAM short description exceeds the reviewed 255-character limit; provide an explicit shorter value",
			Parameters: map[string]any{"current_length": utf8.RuneCountInString(smallDescription), "maximum": maxNameRunes},
		})
	}

	decisions := make([]sites.TargetDecision, 0, 5)
	category := options.Category
	if category > 0 {
		if category > 100_000 {
			return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", "MTEAM category id is outside the supported range", false, nil)
		}
		categoryEvidence := cleanField(options.CategoryEvidence)
		if utf8.RuneCountInString(categoryEvidence) > 500 {
			return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", "MTEAM category evidence exceeds 500 characters", false, nil)
		}
		decisions = append(decisions, sites.TargetDecision{Field: "category", Value: category, Derivation: "explicit_input", Evidence: categoryEvidence})
	} else if strings.EqualFold(material.Source.Tracker, "U2") && (material.Source.AniDBID != "" || material.Links["anidb"] != "") {
		category = 405
		decisions = append(decisions, sites.TargetDecision{
			Field: "category", Value: category, Derivation: "source_site_profile",
			Evidence: "U2 is an anime-focused source profile and this workflow has verified video media.",
		})
	} else {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_category_required", Field: "target_package.category",
			Message:    "MTEAM category cannot be inferred safely for this source; provide the current MTEAM category id",
			Parameters: map[string]any{"category_evidence_field": "target_package.category_evidence"},
		})
	}

	standard := options.Standard
	if standard > 0 {
		if !validStandard(standard) {
			return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", "MTEAM standard must be one of 1, 2, 3, 5, 6, or 7", false, nil)
		}
		decisions = append(decisions, sites.TargetDecision{Field: "standard", Value: standard, Derivation: "explicit_input"})
	} else if inferred, evidence := inferStandard(material.Media.Document); inferred > 0 {
		standard = inferred
		decisions = append(decisions, sites.TargetDecision{Field: "standard", Value: standard, Derivation: "mediainfo", Evidence: evidence})
	} else {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_standard_required", Field: "target_package.standard",
			Message:    "MTEAM resolution standard cannot be derived from MediaInfo; provide a current standard id",
			Parameters: map[string]any{"allowed_values": []int{1, 2, 3, 5, 6, 7}},
		})
	}
	if len(requirements) > 0 {
		return sites.PreparedTargetPackage{}, &sites.PackageRequirementsError{Requirements: requirements}
	}

	for _, screenshot := range material.Screenshots {
		if err := validateImageURL(screenshot.URL); err != nil {
			return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_screenshot_invalid", fmt.Sprintf("screenshot %d URL is invalid", screenshot.Index), false, err)
		}
		if screenshot.ViewerURL != "" {
			if err := validateImageURL(screenshot.ViewerURL); err != nil {
				return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_screenshot_invalid", fmt.Sprintf("screenshot %d viewer URL is invalid", screenshot.Index), false, err)
			}
		}
	}
	anonymous := false
	if options.Anonymous != nil {
		anonymous = *options.Anonymous
	}
	decisions = append(decisions,
		sites.TargetDecision{Field: "name", Value: title, Derivation: stringDerivation(options.Name)},
		sites.TargetDecision{Field: "smallDescr", Value: smallDescription, Derivation: stringDerivation(options.SmallDescription)},
		sites.TargetDecision{Field: "anonymous", Value: anonymous, Derivation: boolDerivation(options.Anonymous)},
	)

	fields := map[string]any{
		"name": title, "smallDescr": smallDescription, "category": category,
		"standard": standard, "anonymous": anonymous,
	}
	if imdb := material.Links["imdb"]; imdb != "" {
		fields["imdb"] = imdb
	}
	if douban := material.Links["douban"]; douban != "" {
		fields["douban"] = douban
	}
	description, err := buildDescription(material, title)
	if err != nil {
		return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_description_invalid", err.Error(), false, err)
	}
	warnings := make([]string, 0, 3)
	if strings.TrimSpace(material.SourceDescription) == "" {
		warnings = append(warnings, "source description is unavailable; review and enrich the target description before upload")
	}
	if len(material.Links) == 0 {
		warnings = append(warnings, "no external metadata link is available; manual identity review is required")
	}
	if len(material.Screenshots) < 3 {
		warnings = append(warnings, "fewer than three uploaded screenshots are available")
	}
	return sites.PreparedTargetPackage{
		SchemaVersion: 1, Target: "MTEAM", Adapter: "mteam_api", Source: material.Source,
		MetadataLinks: cloneStringMap(material.Links), FormFields: fields,
		Description: description, MediaInfo: append(json.RawMessage(nil), material.Media.Document...),
		Content: material.Content, Evidence: material.Evidence, Decisions: decisions, Warnings: warnings,
		ManualReviewRequired: true, GeneratedAt: time.Now().UTC(),
	}, nil
}

func cloneStringMap(input map[string]string) map[string]string {
	result := make(map[string]string, len(input))
	for key, value := range input {
		result[key] = value
	}
	return result
}

func decodeOptions(body json.RawMessage) (packageOptions, error) {
	if len(bytes.TrimSpace(body)) == 0 || bytes.Equal(bytes.TrimSpace(body), []byte("null")) {
		body = json.RawMessage(`{}`)
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var options packageOptions
	if err := decoder.Decode(&options); err != nil {
		return packageOptions{}, fmt.Errorf("decode MTEAM package options: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return packageOptions{}, fmt.Errorf("decode MTEAM package options: trailing JSON value")
	}
	return options, nil
}

func requirement(field, code, message string) sites.PackageRequirement {
	return sites.PackageRequirement{Field: field, Code: code, Message: message}
}

func validStandard(value int) bool {
	switch value {
	case 1, 2, 3, 5, 6, 7:
		return true
	default:
		return false
	}
}

func inferStandard(document json.RawMessage) (int, string) {
	var envelope struct {
		Media struct {
			Track json.RawMessage `json:"track"`
		} `json:"media"`
	}
	if json.Unmarshal(document, &envelope) != nil {
		return 0, ""
	}
	var tracks []map[string]any
	if len(envelope.Media.Track) > 0 && envelope.Media.Track[0] == '[' {
		_ = json.Unmarshal(envelope.Media.Track, &tracks)
	} else {
		var track map[string]any
		if json.Unmarshal(envelope.Media.Track, &track) == nil {
			tracks = []map[string]any{track}
		}
	}
	for _, track := range tracks {
		if !strings.EqualFold(stringValue(track["@type"]), "Video") {
			continue
		}
		height := numericValue(track["Height"])
		width := numericValue(track["Width"])
		scan := strings.ToLower(firstNonEmpty(stringValue(track["ScanType"]), stringValue(track["Scan_Type"])))
		evidence := fmt.Sprintf("video track width=%d height=%d scan_type=%s", width, height, scan)
		switch {
		case width >= 7000 || height >= 4000:
			return 7, evidence
		case width >= 3800 || height >= 2000:
			return 6, evidence
		case height >= 1000 && strings.Contains(scan, "interlac"):
			return 2, evidence
		case height >= 1000:
			return 1, evidence
		case height >= 700:
			return 3, evidence
		case height > 0 || width > 0:
			return 5, evidence
		}
	}
	return 0, ""
}

var digitsPattern = regexp.MustCompile(`[0-9]+`)

func numericValue(value any) int {
	text := stringValue(value)
	digits := strings.Join(digitsPattern.FindAllString(strings.ReplaceAll(text, ",", ""), -1), "")
	result, _ := strconv.Atoi(digits)
	return result
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case json.Number:
		return typed.String()
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	default:
		return ""
	}
}

func buildDescription(material sites.TargetPackageMaterial, title string) (string, error) {
	mediaText, err := safePrettyJSON(material.Media.Document)
	if err != nil {
		return "", fmt.Errorf("format MediaInfo: %w", err)
	}
	lines := []string{"[b]资源信息[/b]", "", "标题：" + safeBBCodeText(title)}
	linkKeys := make([]string, 0, len(material.Links))
	for key := range material.Links {
		linkKeys = append(linkKeys, key)
	}
	sort.Strings(linkKeys)
	for _, key := range linkKeys {
		lines = append(lines, strings.ToUpper(key)+"："+safeBBCodeText(material.Links[key]))
	}
	lines = append(lines, "", "[b]来源证据[/b]",
		"源站："+safeBBCodeText(material.Source.Tracker),
		"源种 ID："+safeBBCodeText(material.Source.TorrentID),
		"源页面："+safeBBCodeText(material.Source.DetailsURL),
	)
	if sourceText := htmlToPlainText(material.SourceDescription); sourceText != "" {
		lines = append(lines, "", "[b]原站简介（已文本化）[/b]", safeBBCodeText(sourceText))
	}
	lines = append(lines, "", "[b]MediaInfo[/b]", "[quote]", mediaText, "[/quote]")
	if len(material.Screenshots) > 0 {
		lines = append(lines, "", "[b]截图[/b]")
		for _, screenshot := range material.Screenshots {
			if screenshot.ViewerURL != "" {
				lines = append(lines, "[url="+screenshot.ViewerURL+"][img]"+screenshot.URL+"[/img][/url]")
			} else {
				lines = append(lines, "[img]"+screenshot.URL+"[/img]")
			}
		}
	}
	lines = append(lines, "", "[b]人工复核义务[/b]", "发布前必须复核命名、分类、查重、转种许可、截图、描述及做种要求。")
	description := strings.TrimSpace(strings.Join(lines, "\n")) + "\n"
	if utf8.RuneCountInString(description) > maxDescriptionRunes {
		return "", fmt.Errorf("MTEAM description exceeds %d characters", maxDescriptionRunes)
	}
	return description, nil
}

func safePrettyJSON(body json.RawMessage) (string, error) {
	var value any
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return "", err
	}
	value = sanitizeJSONStrings(value)
	pretty, err := json.MarshalIndent(value, "", "  ")
	return string(pretty), err
}

func sanitizeJSONStrings(value any) any {
	switch typed := value.(type) {
	case string:
		return safeBBCodeText(typed)
	case []any:
		for index := range typed {
			typed[index] = sanitizeJSONStrings(typed[index])
		}
		return typed
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			result[safeBBCodeText(key)] = sanitizeJSONStrings(item)
		}
		return result
	default:
		return value
	}
}

var (
	htmlDiscardPattern = regexp.MustCompile(`(?is)<(?:script|style|template|noscript)[^>]*>.*?</(?:script|style|template|noscript)\s*>`)
	htmlTagPattern     = regexp.MustCompile(`(?s)<[^>]*>`)
)

func htmlToPlainText(value string) string {
	value = htmlDiscardPattern.ReplaceAllString(value, " ")
	value = htmlTagPattern.ReplaceAllString(value, " ")
	value = html.UnescapeString(value)
	return strings.Join(strings.Fields(value), " ")
}

func safeBBCodeText(value string) string {
	value = strings.Map(func(character rune) rune {
		switch character {
		case '[':
			return '［'
		case ']':
			return '］'
		case '\x00':
			return -1
		default:
			if unicode.IsControl(character) && character != '\n' && character != '\t' {
				return ' '
			}
			return character
		}
	}, value)
	return strings.TrimSpace(value)
}

func cleanField(value string) string {
	value = strings.Map(func(character rune) rune {
		if unicode.IsControl(character) {
			return ' '
		}
		return character
	}, value)
	value = strings.Join(strings.Fields(value), " ")
	return strings.TrimSpace(value)
}

func validateImageURL(value string) error {
	if strings.ContainsAny(value, "[]\r\n\t ") {
		return fmt.Errorf("unsafe URL characters")
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
		return fmt.Errorf("image URL must be absolute HTTPS without credentials or fragment")
	}
	return nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func stringDerivation(override string) string {
	if strings.TrimSpace(override) != "" {
		return "explicit_input"
	}
	return "source_metadata"
}

func boolDerivation(override *bool) string {
	if override != nil {
		return "explicit_input"
	}
	return "adapter_default"
}

var _ sites.TargetPackageAdapter = PackageAdapter{}
