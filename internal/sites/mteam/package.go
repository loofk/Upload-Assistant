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

	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
)

const (
	maxNameRunes        = 255
	maxDescriptionRunes = 1_000_000
)

type PackageAdapter struct{}

var (
	seasonEpisodeToken = regexp.MustCompile(`(?i)(?:^|[ ._-])S[0-9]{1,2}E[0-9]{1,3}(?:-E[0-9]{1,3})?(?:$|[ ._-])`)
	seasonToken        = regexp.MustCompile(`(?i)(?:^|[ ._-])S[0-9]{1,2}(?:$|[ ._-])`)
	releaseYearToken   = regexp.MustCompile(`(?:^|[ ._-])((?:19|20)[0-9]{2})(?:$|[ ._-])`)
	resolutionToken    = regexp.MustCompile(`(?i)(?:^|[ ._-])((?:4320|2160|1440|1080|720|576|480)[pi])(?:$|[ ._-])`)
	releaseSourceToken = regexp.MustCompile(`(?i)(?:^|[ ._-])(UHD[ ._-]?BluRay|BluRay|BDRip|WEB[ ._-]?DL|WEBRip|HDTV|DVDRip|DVD)(?:$|[ ._-])`)
	videoCodecToken    = regexp.MustCompile(`(?i)(?:^|[ ._-])(x26[45]|H[ ._-]?26[45]|AVC|HEVC|AV1|MPEG-?2)(?:$|[ ._-])`)
	audioCodecToken    = regexp.MustCompile(`(?i)(?:^|[ ._-])(AAC|AC-?3|E-?AC-?3|DDP?|DTS(?:-HD)?|TrueHD|FLAC|LPCM|Opus)(?:[ ._-]?(Atmos|MA|X))?(?:$|[ ._-])`)
	audioChannelsToken = regexp.MustCompile(`(?i)(?:^|[ ._-])([0-9](?:\.[0-9])?)(?:CH)?(?:$|[ ._-])`)
	hdrToken           = regexp.MustCompile(`(?i)(?:^|[ ._-])(HDR10\+?|DV|DoVi|Dolby[ ._-]?Vision|HLG)(?:$|[ ._-])`)
	releaseTypeToken   = regexp.MustCompile(`(?i)(?:^|[ ._-])(REMUX|Complete|Internal|Repack|Proper|Hybrid)(?:$|[ ._-])`)
	// The delimiter is the final dash. Excluding dashes from the captured group
	// prevents WEB-DL-GROUP from being misread as group "DL-GROUP".
	releaseGroupToken = regexp.MustCompile(`-([A-Za-z0-9][A-Za-z0-9._]{0,31})$`)
)

func NewPackageAdapter() PackageAdapter { return PackageAdapter{} }

func (PackageAdapter) SiteCode() string { return "MTEAM" }

type packageOptions struct {
	Name             string `json:"name,omitempty"`
	NamingProfile    string `json:"naming_profile,omitempty"`
	SmallDescription string `json:"small_descr,omitempty"`
	Description      string `json:"description,omitempty"`
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
	sourceTitle := cleanField(firstNonEmpty(material.Title, material.Source.Name))
	title := cleanField(firstNonEmpty(options.Name, sourceTitle))
	namingProfile := cleanField(options.NamingProfile)
	namingProfileDerivation := stringDerivation(options.NamingProfile)
	namingProfileEvidence := ""
	if namingProfile == "" && len(material.Naming.Profiles) > 0 {
		namingProfile, namingProfileEvidence = inferNamingProfile(material, title)
		if namingProfile != "" {
			namingProfileDerivation = "verified_metadata"
		}
	}
	smallDescription := cleanField(firstNonEmpty(options.SmallDescription, material.Title, material.Source.Name))
	requirements := make([]sites.PackageRequirement, 0, 5)
	if utf8.RuneCountInString(namingProfile) > 64 {
		return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", "MTEAM naming profile exceeds 64 characters", false, nil)
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
	if namingProfile == "" && len(material.Naming.Profiles) > 0 {
		selected, selectErr := rules.SelectNamingProfile(material.Naming, "", resourceClassForMaterial(material, sourceTitle), category)
		if selectErr == nil {
			namingProfile = selected.ID
			namingProfileDerivation = "verified_category"
			namingProfileEvidence = "active rule selector matched verified target category or media class"
		}
	}
	if len(material.Naming.Profiles) > 0 && namingProfile == "" {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_naming_profile_required", Field: "target_package.naming_profile",
			Message:    "active MTEAM rules could not select one naming profile from the verified category and metadata",
			Parameters: map[string]any{"allowed_profiles": namingProfileIDs(material.Naming.Profiles), "category": category},
		})
	} else if options.Name == "" && namingProfile != "" {
		if profile, selectErr := rules.SelectNamingProfile(material.Naming, namingProfile, "", category); selectErr == nil && len(profile.TitleTokens) > 0 {
			generated, missing := rules.RenderNamingTitle(profile, releaseTokenValues(sourceTitle))
			if len(missing) > 0 {
				requirements = append(requirements, sites.PackageRequirement{
					Code: "target_naming_tokens_required", Field: "target_package.name",
					Message:    "the reviewed naming template requires release fields that could not be verified",
					Parameters: map[string]any{"profile": profile.ID, "missing_tokens": missing, "source_title": sourceTitle},
				})
			} else {
				title = generated
			}
		}
	}
	if title == "" {
		requirements = append(requirements, requirement("target_package.name", "target_name_required", "MTEAM requires a non-empty release name"))
	} else if utf8.RuneCountInString(title) > maxNameRunes {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_name_too_long", Field: "target_package.name",
			Message:    "MTEAM release name exceeds the reviewed 255-character limit; provide an explicit shorter name",
			Parameters: map[string]any{"current_length": utf8.RuneCountInString(title), "maximum": maxNameRunes},
		})
	}

	standard := options.Standard
	if standard > 0 {
		if !validStandard(standard) {
			return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", "MTEAM standard must be one of 1, 2, 3, 5, 6, or 7", false, nil)
		}
		decisions = append(decisions, sites.TargetDecision{Field: "standard", Value: standard, Derivation: "explicit_input"})
	} else if inferred, evidence := inferStandard(material.Media.Kind, material.Media.Document); inferred > 0 {
		standard = inferred
		decisions = append(decisions, sites.TargetDecision{Field: "standard", Value: standard, Derivation: strings.ToLower(material.Media.Kind), Evidence: evidence})
	} else {
		requirements = append(requirements, sites.PackageRequirement{
			Code: "target_standard_required", Field: "target_package.standard",
			Message:    "MTEAM resolution standard cannot be derived from MediaInfo; provide a current standard id",
			Parameters: map[string]any{"allowed_values": []int{1, 2, 3, 5, 6, 7}},
		})
	}
	if material.MetadataEnrichmentRequired {
		for _, required := range []struct {
			key, code, message string
		}{
			{"imdb", "target_imdb_required", "MTEAM reference workflow requires a verified IMDb link"},
			{"tmdb", "target_tmdb_required", "MTEAM reference workflow requires a verified typed TMDb link"},
			{"douban", "target_douban_required", "MTEAM reference workflow requires a verified Douban subject link"},
		} {
			if strings.TrimSpace(material.Links[required.key]) == "" {
				requirements = append(requirements, requirement("metadata."+required.key, required.code, required.message))
			}
		}
		if strings.TrimSpace(material.MetadataDescription) == "" {
			requirements = append(requirements, requirement("metadata.ptgen_description", "target_ptgen_description_required", "MTEAM reference workflow requires a verified PTGen/Douban description"))
		}
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
	nameDerivation := stringDerivation(options.Name)
	if options.Name == "" && title != sourceTitle {
		nameDerivation = "active_rule_template"
	}
	decisions = append(decisions,
		sites.TargetDecision{Field: "name", Value: title, Derivation: nameDerivation},
		sites.TargetDecision{Field: "namingProfile", Value: namingProfile, Derivation: namingProfileDerivation, Evidence: namingProfileEvidence},
		sites.TargetDecision{Field: "smallDescr", Value: smallDescription, Derivation: stringDerivation(options.SmallDescription)},
		sites.TargetDecision{Field: "anonymous", Value: anonymous, Derivation: boolDerivation(options.Anonymous)},
	)

	fields := map[string]any{
		"name": title, "namingProfile": namingProfile, "smallDescr": smallDescription, "category": category,
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
	if override := strings.TrimSpace(options.Description); override != "" {
		if !utf8.ValidString(override) || strings.IndexByte(override, 0) >= 0 || utf8.RuneCountInString(override) > maxDescriptionRunes {
			return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_package_options_invalid", "MTEAM description override is invalid or too large", false, nil)
		}
		description = override
		decisions = append(decisions, sites.TargetDecision{Field: "description", Value: "operator_override", Derivation: "explicit_input"})
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
	mediaPayload, err := json.Marshal(material.Media.Document)
	if err != nil {
		return sites.PreparedTargetPackage{}, sites.NewAdapterError("target_media_evidence_invalid", "media evidence could not be serialized", false, err)
	}
	return sites.PreparedTargetPackage{
		SchemaVersion: 1, Target: "MTEAM", Adapter: "mteam_api", Source: material.Source,
		MetadataLinks: cloneStringMap(material.Links), FormFields: fields,
		Description: description, MediaInfo: mediaPayload,
		Content: material.Content, Evidence: material.Evidence, Decisions: decisions, Warnings: warnings,
		NamingProfiles:       append([]rules.NamingProfile(nil), material.Naming.Profiles...),
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

func inferStandard(kind, document string) (int, string) {
	if strings.EqualFold(strings.TrimSpace(kind), "bdinfo") {
		return inferBDInfoStandard(document)
	}
	var envelope struct {
		Media struct {
			Track json.RawMessage `json:"track"`
		} `json:"media"`
	}
	if json.Unmarshal([]byte(document), &envelope) != nil {
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

var bdinfoVideoResolutionPattern = regexp.MustCompile(`(?i)^\s*Video:\s*.*\b(4320|2160|1080|720)([pi])\b`)

func inferBDInfoStandard(document string) (int, string) {
	bestHeight, bestScan, bestLine := 0, "", ""
	for _, line := range strings.Split(document, "\n") {
		match := bdinfoVideoResolutionPattern.FindStringSubmatch(line)
		if len(match) != 3 {
			continue
		}
		height, _ := strconv.Atoi(match[1])
		if height > bestHeight || (height == bestHeight && strings.EqualFold(match[2], "p")) {
			bestHeight, bestScan, bestLine = height, strings.ToLower(match[2]), strings.TrimSpace(line)
		}
	}
	evidence := safeBBCodeText(bestLine)
	switch {
	case bestHeight >= 4320:
		return 7, evidence
	case bestHeight >= 2160:
		return 6, evidence
	case bestHeight >= 1080 && bestScan == "i":
		return 2, evidence
	case bestHeight >= 1080:
		return 1, evidence
	case bestHeight >= 720:
		return 3, evidence
	default:
		return 0, ""
	}
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
	mediaText, err := formatMediaEvidence(material.Media)
	if err != nil {
		return "", fmt.Errorf("format media evidence: %w", err)
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
	if metadataText := safeBBCodeText(material.MetadataDescription); metadataText != "" {
		lines = append(lines, "", "[b]豆瓣/PTGen 简介（已安全文本化）[/b]", metadataText)
	}
	mediaLabel := "MediaInfo"
	if strings.EqualFold(material.Media.Kind, "bdinfo") {
		mediaLabel = "BDInfo"
	}
	lines = append(lines, "", "[b]"+mediaLabel+"[/b]", "[quote]", mediaText, "[/quote]")
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

func formatMediaEvidence(media sites.TargetMediaEvidence) (string, error) {
	switch strings.ToLower(strings.TrimSpace(media.Kind)) {
	case "bdinfo":
		text := safeBBCodeText(media.Document)
		if text == "" {
			return "", fmt.Errorf("BDInfo report is empty")
		}
		return text, nil
	case "mediainfo":
		return safePrettyJSON([]byte(media.Document))
	default:
		return "", fmt.Errorf("unsupported media evidence kind %q", media.Kind)
	}
}

func safePrettyJSON(body []byte) (string, error) {
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

func inferNamingProfile(material sites.TargetPackageMaterial, title string) (string, string) {
	has := func(id string) bool {
		for _, profile := range material.Naming.Profiles {
			if profile.ID == id {
				return true
			}
		}
		return false
	}
	mediaKind := strings.ToLower(strings.TrimSpace(material.Media.Kind))
	tmdbType := strings.ToLower(strings.TrimSpace(material.Source.TMDbType))
	upperTitle := strings.ToUpper(title)
	if tmdbType == "movie" && has("movie") {
		return "movie", "verified TMDb media type is movie"
	}
	if tmdbType == "tv" {
		if seasonEpisodeToken.MatchString(title) && has("tv_episode") {
			return "tv_episode", "verified TMDb media type is TV and the title contains a season/episode token"
		}
		if seasonToken.MatchString(title) && has("tv_season") {
			return "tv_season", "verified TMDb media type is TV and the title contains a season token"
		}
	}
	if material.Source.AniDBID != "" || strings.EqualFold(material.Source.Tracker, "U2") {
		if mediaKind == "bdinfo" && has("anime_disc") {
			return "anime_disc", "verified anime identity and BDInfo media evidence"
		}
		if strings.Contains(upperTitle, "WEB-DL") {
			if seasonEpisodeToken.MatchString(title) && has("anime_web_episode") {
				return "anime_web_episode", "verified anime identity, WEB-DL title, and season/episode token"
			}
			if seasonToken.MatchString(title) && has("anime_web_season") {
				return "anime_web_season", "verified anime identity, WEB-DL title, and season token"
			}
		}
		if (strings.Contains(upperTitle, "BLURAY") || strings.Contains(upperTitle, "BDRIP")) && has("anime_encode") {
			return "anime_encode", "verified anime identity and encoded BluRay/BDRip title"
		}
	}
	return "", ""
}

func resourceClassForMaterial(material sites.TargetPackageMaterial, title string) string {
	mediaKind := strings.ToLower(strings.TrimSpace(material.Media.Kind))
	if material.Source.AniDBID != "" || strings.EqualFold(material.Source.Tracker, "U2") {
		upper := strings.ToUpper(title)
		if mediaKind == "bdinfo" {
			return "anime_disc"
		}
		if strings.Contains(upper, "WEB-DL") || strings.Contains(upper, "WEB DL") {
			if seasonEpisodeToken.MatchString(title) {
				return "anime_web_episode"
			}
			return "anime_web_season"
		}
		return "anime_encode"
	}
	if strings.EqualFold(material.Source.TMDbType, "movie") {
		return "movie"
	}
	if strings.EqualFold(material.Source.TMDbType, "tv") {
		if seasonEpisodeToken.MatchString(title) {
			return "tv_episode"
		}
		return "tv_season"
	}
	return ""
}

func namingProfileIDs(profiles []rules.NamingProfile) []string {
	result := make([]string, 0, len(profiles))
	for _, profile := range profiles {
		result = append(result, profile.ID)
	}
	return result
}

func releaseTokenValues(title string) map[string]string {
	values := map[string]string{}
	remaining := strings.TrimSpace(title)
	if match := releaseGroupToken.FindStringSubmatch(remaining); len(match) > 1 {
		values["group"] = match[1]
		remaining = strings.TrimSpace(strings.TrimSuffix(remaining, match[0]))
	}
	// Remove each matched token by index instead of interpreting it as a
	// regular-expression replacement. This preserves punctuation in titles.
	extractOne := func(key string, pattern *regexp.Regexp) {
		indices := pattern.FindStringSubmatchIndex(remaining)
		if len(indices) < 4 {
			return
		}
		value := strings.TrimSpace(remaining[indices[2]:indices[3]])
		if key == "audio_codec" && len(indices) >= 6 && indices[4] >= 0 {
			value += "-" + strings.TrimSpace(remaining[indices[4]:indices[5]])
		}
		values[key] = value
		remaining = remaining[:indices[0]] + " " + remaining[indices[1]:]
	}
	extractOne("season_episode", regexp.MustCompile(`(?i)(?:^|[ ._-])(S[0-9]{1,3}(?:E[0-9]{1,4}(?:-E[0-9]{1,4})?)?)(?:$|[ ._-])`))
	extractOne("year", releaseYearToken)
	extractOne("resolution", resolutionToken)
	extractOne("source", releaseSourceToken)
	extractOne("release_type", releaseTypeToken)
	extractOne("video_codec", videoCodecToken)
	extractOne("audio_codec", audioCodecToken)
	extractOne("audio_channels", audioChannelsToken)
	extractOne("hdr", hdrToken)
	remaining = strings.Trim(strings.TrimSpace(strings.NewReplacer(".", " ", "_", " ").Replace(remaining)), "- ")
	remaining = strings.Join(strings.Fields(remaining), " ")
	if remaining != "" {
		values["title"] = remaining
	}
	return values
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
