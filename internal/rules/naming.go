package rules

import (
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

type NamingViolation struct {
	Code            string
	Field           string
	Message         string
	Profile         string
	AllowedProfiles []string
	Pattern         string
	Template        string
	MaxLength       int
}

// CheckNaming evaluates only reviewed deterministic constraints. A missing or
// mismatching mandatory value is a hard pre-upload gate; advisory naming text
// belongs in Policy.Advisories instead.
func CheckNaming(naming Naming, releaseTitle, contentName string) []NamingViolation {
	return CheckNamingProfile(naming, "", releaseTitle, contentName)
}

// CheckNamingProfile applies the global constraints and, when configured, the
// explicitly selected resource-class profile. Multiple profiles fail closed:
// the workflow must persist a reviewed selection instead of guessing one.
func CheckNamingProfile(naming Naming, profileID, releaseTitle, contentName string) []NamingViolation {
	result := make([]NamingViolation, 0, 2)
	result = append(result, checkNamingConstraint("target_package.name", "target_release_title", releaseTitle, naming.ReleaseTitle)...)
	result = append(result, checkNamingConstraint("content.root_name", "target_content_name", contentName, naming.ContentName)...)
	if len(naming.Profiles) == 0 {
		return result
	}
	profileID = strings.TrimSpace(profileID)
	allowed := make([]string, 0, len(naming.Profiles))
	var selected *NamingProfile
	for index := range naming.Profiles {
		profile := &naming.Profiles[index]
		allowed = append(allowed, profile.ID)
		if profile.ID == profileID {
			selected = profile
		}
	}
	if selected == nil {
		code, message := "target_naming_profile_required", "active target rules require an explicit naming profile"
		if profileID != "" {
			code, message = "target_naming_profile_invalid", "selected naming profile is not present in the active target rules"
		}
		return append(result, NamingViolation{
			Code: code, Field: "target_package.naming_profile", Message: message,
			Profile: profileID, AllowedProfiles: allowed,
		})
	}
	profileViolations := checkNamingConstraint("target_package.name", "target_release_title", releaseTitle, selected.ReleaseTitle)
	for index := range profileViolations {
		profileViolations[index].Profile = selected.ID
		profileViolations[index].AllowedProfiles = allowed
	}
	result = append(result, profileViolations...)
	return result
}

func checkNamingConstraint(field, codePrefix, value string, constraint NamingConstraint) []NamingViolation {
	if !constraint.Required {
		return nil
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return []NamingViolation{{
			Code: codePrefix + "_required", Field: field,
			Message: "active target rules require a non-empty reviewed name",
			Pattern: constraint.Pattern, Template: constraint.Template, MaxLength: constraint.MaxLength,
		}}
	}
	if constraint.MaxLength > 0 && utf8.RuneCountInString(value) > constraint.MaxLength {
		return []NamingViolation{{
			Code: codePrefix + "_too_long", Field: field,
			Message: fmt.Sprintf("name exceeds the active rule maximum of %d characters", constraint.MaxLength),
			Pattern: constraint.Pattern, Template: constraint.Template, MaxLength: constraint.MaxLength,
		}}
	}
	compiled, err := regexp.Compile(constraint.Pattern)
	if err != nil || !compiled.MatchString(value) {
		return []NamingViolation{{
			Code: codePrefix + "_mismatch", Field: field,
			Message: "name does not match the active target naming rule",
			Pattern: constraint.Pattern, Template: constraint.Template, MaxLength: constraint.MaxLength,
		}}
	}
	return nil
}
