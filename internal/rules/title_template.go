package rules

import (
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// CompileNamingTemplates replaces model-authored profile patterns with a
// deterministic server-owned pattern whenever semantic title tokens exist.
func CompileNamingTemplates(naming *Naming) error {
	if naming == nil {
		return nil
	}
	for index := range naming.Profiles {
		profile := &naming.Profiles[index]
		if len(profile.TitleTokens) == 0 {
			continue
		}
		pattern, template, err := compileTitleTokens(profile.TitleTokens)
		if err != nil {
			return fmt.Errorf("compile naming profile %s: %w", profile.ID, err)
		}
		profile.ReleaseTitle.Required = true
		profile.ReleaseTitle.Pattern = pattern
		profile.ReleaseTitle.Template = template
	}
	return nil
}

func compileTitleTokens(tokens []NamingToken) (string, string, error) {
	patterns := map[string]string{
		"title": `.+?`, "year": `(?:19|20)[0-9]{2}`,
		"season_episode": `S[0-9]{1,3}(?:E[0-9]{1,4}(?:-E[0-9]{1,4})?)?`,
		"resolution":     `(?:4320|2160|1440|1080|720|576|480)[pi]`,
		"source":         `(?:UHD[ ._-]?BluRay|BluRay|BDRip|WEB[ ._-]?DL|WEBRip|HDTV|DVDRip|DVD)`,
		"release_type":   `(?:REMUX|Complete|Internal|Repack|Proper|Hybrid)`,
		"video_codec":    `(?:x26[45]|H[ ._-]?26[45]|AVC|HEVC|AV1|MPEG-?2)`,
		"audio_codec":    `(?:AAC|AC-?3|E-?AC-?3|DDP?|DTS(?:-HD)?|TrueHD|FLAC|LPCM|Opus)(?:[ ._-]?(?:Atmos|MA|X))?`,
		"audio_channels": `[0-9](?:\.[0-9])?`,
		"hdr":            `(?:HDR10\+?|DV|DoVi|Dolby[ ._-]?Vision|HLG)`,
		"language":       `[A-Za-z]{2,12}(?:\+[A-Za-z]{2,12})*`,
		"edition":        `[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}`,
		"group":          `[A-Za-z0-9][A-Za-z0-9._-]{0,31}`,
	}
	var pattern, template strings.Builder
	pattern.WriteString("^")
	for _, token := range tokens {
		separator := token.Separator
		if separator == "" && pattern.Len() > 1 {
			separator = " "
		}
		if token.Kind == "literal" {
			pattern.WriteString(regexp.QuoteMeta(token.Value))
			template.WriteString(token.Value)
			continue
		}
		fieldPattern, ok := patterns[token.Value]
		if !ok {
			return "", "", fmt.Errorf("unsupported field %q", token.Value)
		}
		part := regexp.QuoteMeta(separator) + "(?:" + fieldPattern + ")"
		text := separator + "{" + token.Value + "}"
		if !token.Required {
			part = "(?:" + part + ")?"
			text = "[" + text + "]"
		}
		pattern.WriteString(part)
		template.WriteString(text)
	}
	pattern.WriteString("$")
	return pattern.String(), strings.TrimSpace(template.String()), nil
}

// RenderNamingTitle renders one reviewed profile using already verified token
// values. It returns missing required tokens instead of inventing replacements.
func RenderNamingTitle(profile NamingProfile, values map[string]string) (string, []string) {
	var title strings.Builder
	missing := make([]string, 0)
	for _, token := range profile.TitleTokens {
		if token.Kind == "literal" {
			title.WriteString(token.Value)
			continue
		}
		value := strings.TrimSpace(values[token.Value])
		if value == "" {
			if token.Required {
				missing = append(missing, token.Value)
			}
			continue
		}
		separator := token.Separator
		if separator == "" && title.Len() > 0 {
			separator = " "
		}
		title.WriteString(separator)
		title.WriteString(value)
	}
	return strings.TrimSpace(title.String()), slices.Compact(missing)
}

// SelectNamingProfile uses an explicit selection first, then exact native
// category ids, then verified canonical resource classes. Ambiguity fails
// closed. A single selector-free legacy profile remains selectable.
func SelectNamingProfile(naming Naming, explicitID, resourceClass string, categoryID int) (NamingProfile, error) {
	if explicitID != "" {
		for _, profile := range naming.Profiles {
			if profile.ID == explicitID {
				return profile, nil
			}
		}
		return NamingProfile{}, fmt.Errorf("selected naming profile %q is unavailable", explicitID)
	}
	matching := make([]NamingProfile, 0)
	if categoryID > 0 {
		for _, profile := range naming.Profiles {
			if slices.Contains(profile.CategoryIDs, categoryID) {
				matching = append(matching, profile)
			}
		}
		if len(matching) == 1 {
			return matching[0], nil
		}
		if len(matching) > 1 {
			return NamingProfile{}, fmt.Errorf("category %s matches multiple naming profiles", strconv.Itoa(categoryID))
		}
	}
	resourceClass = strings.TrimSpace(resourceClass)
	if resourceClass != "" {
		for _, profile := range naming.Profiles {
			if slices.Contains(profile.ResourceClasses, resourceClass) {
				matching = append(matching, profile)
			}
		}
		if len(matching) == 1 {
			return matching[0], nil
		}
		if len(matching) > 1 {
			return NamingProfile{}, fmt.Errorf("resource class %q matches multiple naming profiles", resourceClass)
		}
	}
	if len(naming.Profiles) == 1 && len(naming.Profiles[0].CategoryIDs) == 0 && len(naming.Profiles[0].ResourceClasses) == 0 {
		return naming.Profiles[0], nil
	}
	return NamingProfile{}, fmt.Errorf("naming profile cannot be selected unambiguously")
}
