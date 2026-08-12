package operations

import (
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/rules"
)

const ruleAnalysisChunkBytes = 256 << 10

// splitRuleAnalysisSource keeps normal rule sets in one call and only splits
// when the prompt would otherwise become a model-context gamble. It prefers
// line boundaries so source evidence references remain intact.
func splitRuleAnalysisSource(source string) []string {
	if len(source) <= ruleAnalysisChunkBytes {
		return []string{source}
	}
	chunks := []string{}
	var current strings.Builder
	flush := func() {
		if current.Len() == 0 {
			return
		}
		chunks = append(chunks, strings.TrimSpace(current.String()))
		current.Reset()
	}
	for _, line := range strings.SplitAfter(source, "\n") {
		for len(line) > ruleAnalysisChunkBytes {
			if current.Len() > 0 {
				flush()
			}
			cut := ruleAnalysisChunkBytes
			for cut > 0 && !utf8.RuneStart(line[cut]) {
				cut--
			}
			if cut == 0 {
				cut = ruleAnalysisChunkBytes
			}
			chunks = append(chunks, strings.TrimSpace(line[:cut]))
			line = line[cut:]
		}
		if current.Len() > 0 && len(line) > ruleAnalysisChunkBytes-current.Len() {
			flush()
		}
		current.WriteString(line)
	}
	flush()
	return chunks
}

func mergeRuleExtractions(values []ruleExtraction) ruleExtraction {
	if len(values) == 0 {
		return ruleExtraction{}
	}
	merged := ruleExtraction{}
	confidenceSet := false
	for _, value := range values {
		merged.Automation.Download = merged.Automation.Download || value.Automation.Download
		merged.Automation.Upload = merged.Automation.Upload || value.Automation.Upload
		merged.Automation.Retorrent = merged.Automation.Retorrent || value.Automation.Retorrent
		mergeAccess(&merged, value.Access)
		merged.Limits.Download = mergeProviderRate(merged.Limits.Download, value.Limits.Download)
		merged.Limits.Upload = mergeProviderRate(merged.Limits.Upload, value.Limits.Upload)
		merged.Limits.SeedboxUpload = mergeProviderRate(merged.Limits.SeedboxUpload, value.Limits.SeedboxUpload)
		mergeNaming(&merged, value.Naming)
		if value.Seeding.MinimumTimeHours > merged.Seeding.MinimumTimeHours {
			merged.Seeding.MinimumTimeHours = value.Seeding.MinimumTimeHours
		}
		if value.Seeding.MinimumRatio > merged.Seeding.MinimumRatio {
			merged.Seeding.MinimumRatio = value.Seeding.MinimumRatio
		}
		merged.Transfer.FreeleechRequired = merged.Transfer.FreeleechRequired || value.Transfer.FreeleechRequired
		merged.Transfer.ForbidOriginalTorrent = merged.Transfer.ForbidOriginalTorrent || value.Transfer.ForbidOriginalTorrent
		merged.Transfer.PreserveContent = merged.Transfer.PreserveContent || value.Transfer.PreserveContent
		merged.Transfer.RequiredPromotions = appendUniqueStrings(merged.Transfer.RequiredPromotions, value.Transfer.RequiredPromotions, 64)
		merged.Transfer.ForbiddenTitlePatterns = appendUniqueStrings(merged.Transfer.ForbiddenTitlePatterns, value.Transfer.ForbiddenTitlePatterns, 64)
		merged.Transfer.ForbiddenReleaseGroups = appendUniqueStrings(merged.Transfer.ForbiddenReleaseGroups, value.Transfer.ForbiddenReleaseGroups, 64)
		merged.Obligations = appendUniqueJSON(merged.Obligations, value.Obligations, 128)
		merged.Advisories = appendUniqueJSON(merged.Advisories, value.Advisories, 256)
		merged.Notes = appendUniqueStrings(merged.Notes, value.Notes, 128)
		merged.Warnings = appendUniqueStrings(merged.Warnings, value.Warnings, 128)
		merged.Conflicts = appendUniqueJSON(merged.Conflicts, value.Conflicts, 32)
		if value.Confidence > 0 && (!confidenceSet || value.Confidence < merged.Confidence) {
			merged.Confidence = value.Confidence
			confidenceSet = true
		}
	}
	if len(values) > 1 {
		merged.Warnings = appendUniqueStrings(merged.Warnings, []string{fmt.Sprintf("原文超过单次上下文预算，服务已按证据行分为 %d 段提取并保守合并；请重点核对跨段冲突。", len(values))}, 128)
	}
	return merged
}

func mergeAccess(merged *ruleExtraction, incoming rules.Access) {
	merged.Access.ServiceAccess = mergeAccessDecision(merged, "access.service_access", merged.Access.ServiceAccess, incoming.ServiceAccess)
	merged.Access.SearchAccess = mergeAccessDecision(merged, "access.search_access", merged.Access.SearchAccess, incoming.SearchAccess)
	merged.Access.GeneralMinIntervalSeconds = maxPositive(merged.Access.GeneralMinIntervalSeconds, incoming.GeneralMinIntervalSeconds)
	merged.Access.SearchMinIntervalSeconds = maxPositive(merged.Access.SearchMinIntervalSeconds, incoming.SearchMinIntervalSeconds)
	merged.Access.GeneralMaxRequestsPerHour = minPositive(merged.Access.GeneralMaxRequestsPerHour, incoming.GeneralMaxRequestsPerHour)
	merged.Access.SearchMaxRequestsPerHour = minPositive(merged.Access.SearchMaxRequestsPerHour, incoming.SearchMaxRequestsPerHour)
	merged.Access.MaxConcurrency = minPositive(merged.Access.MaxConcurrency, incoming.MaxConcurrency)
}

func mergeAccessDecision(merged *ruleExtraction, section, current, incoming string) string {
	current = strings.TrimSpace(current)
	incoming = strings.TrimSpace(incoming)
	if current == "" || current == "undetermined" {
		if containsMergeConflictWarning(merged.Warnings, section) {
			return "undetermined"
		}
		return incoming
	}
	if incoming == "" || incoming == "undetermined" || incoming == current {
		return current
	}
	merged.Warnings = appendUniqueStrings(merged.Warnings, []string{section + " 在不同原文分段中互相矛盾，已降级为 undetermined。"}, 128)
	return "undetermined"
}

func containsMergeConflictWarning(warnings []string, section string) bool {
	prefix := section + " 在不同原文分段中互相矛盾"
	for _, warning := range warnings {
		if strings.HasPrefix(warning, prefix) {
			return true
		}
	}
	return false
}

func mergeProviderRate(current, incoming providerRate) providerRate {
	candidates := append(rateCandidates(current), rateCandidates(incoming)...)
	unique := make([]providerRateCandidate, 0, len(candidates))
	indexes := map[string]int{}
	for _, candidate := range candidates {
		candidate.Declared = strings.TrimSpace(candidate.Declared)
		candidate.Scope = strings.TrimSpace(candidate.Scope)
		if candidate.Declared == "" {
			continue
		}
		key := candidate.Declared + "\x00" + candidate.Scope
		if index, exists := indexes[key]; exists {
			unique[index].EvidenceRefs = appendUniqueStrings(unique[index].EvidenceRefs, candidate.EvidenceRefs, 32)
			continue
		}
		candidate.EvidenceRefs = appendUniqueStrings(nil, candidate.EvidenceRefs, 32)
		indexes[key] = len(unique)
		unique = append(unique, candidate)
	}
	if len(unique) == 0 {
		return providerRate{}
	}
	result := providerRate{Declared: unique[0].Declared, Scope: unique[0].Scope, EvidenceRefs: unique[0].EvidenceRefs}
	if len(unique) > 1 {
		result.Alternatives = append([]providerRateCandidate(nil), unique[1:]...)
	}
	return result
}

func rateCandidates(rate providerRate) []providerRateCandidate {
	values := append([]providerRateCandidate(nil), rate.Alternatives...)
	if strings.TrimSpace(rate.Declared) != "" {
		values = append([]providerRateCandidate{{Declared: rate.Declared, Scope: rate.Scope, EvidenceRefs: rate.EvidenceRefs}}, values...)
	}
	return values
}

func mergeNaming(merged *ruleExtraction, incoming rules.Naming) {
	merged.Naming.ReleaseTitle = mergeNamingConstraint(merged, "naming.release_title", merged.Naming.ReleaseTitle, incoming.ReleaseTitle)
	merged.Naming.ContentName = mergeNamingConstraint(merged, "naming.content_name", merged.Naming.ContentName, incoming.ContentName)
	profiles := map[string]int{}
	conflicted := map[string]bool{}
	for index, profile := range merged.Naming.Profiles {
		profiles[profile.ID] = index
	}
	for _, profile := range incoming.Profiles {
		if conflicted[profile.ID] {
			continue
		}
		index, exists := profiles[profile.ID]
		if !exists {
			if len(merged.Naming.Profiles) < 32 {
				profiles[profile.ID] = len(merged.Naming.Profiles)
				merged.Naming.Profiles = append(merged.Naming.Profiles, profile)
			}
			continue
		}
		current := merged.Naming.Profiles[index]
		if namingProfilesEquivalent(current, profile) {
			current.ReleaseTitle.EvidenceRefs = appendUniqueStrings(current.ReleaseTitle.EvidenceRefs, profile.ReleaseTitle.EvidenceRefs, 32)
			current.ResourceClasses = appendUniqueStrings(current.ResourceClasses, profile.ResourceClasses, 64)
			current.CategoryIDs = appendUniqueInts(current.CategoryIDs, profile.CategoryIDs, 128)
			merged.Naming.Profiles[index] = current
			continue
		}
		refs := appendUniqueStrings(current.ReleaseTitle.EvidenceRefs, profile.ReleaseTitle.EvidenceRefs, 32)
		if len(refs) >= 2 && len(merged.Conflicts) < 32 {
			merged.Conflicts = append(merged.Conflicts, rules.SourceConflict{Section: "naming", Summary: "命名配置档 " + profile.ID + " 在不同原文分段中不一致", EvidenceRefs: refs})
		} else {
			merged.Warnings = appendUniqueStrings(merged.Warnings, []string{"命名配置档 " + profile.ID + " 在不同原文分段中不一致且缺少足够证据引用，已省略。"}, 128)
		}
		merged.Naming.Profiles = append(merged.Naming.Profiles[:index], merged.Naming.Profiles[index+1:]...)
		profiles = map[string]int{}
		for nextIndex, item := range merged.Naming.Profiles {
			profiles[item.ID] = nextIndex
		}
		conflicted[profile.ID] = true
	}
}

func mergeNamingConstraint(merged *ruleExtraction, section string, current, incoming rules.NamingConstraint) rules.NamingConstraint {
	if namingConstraintEmpty(current) {
		return incoming
	}
	if namingConstraintEmpty(incoming) {
		return current
	}
	if namingConstraintsEquivalent(current, incoming) {
		current.EvidenceRefs = appendUniqueStrings(current.EvidenceRefs, incoming.EvidenceRefs, 32)
		return current
	}
	refs := appendUniqueStrings(current.EvidenceRefs, incoming.EvidenceRefs, 32)
	if len(refs) >= 2 && len(merged.Conflicts) < 32 {
		merged.Conflicts = append(merged.Conflicts, rules.SourceConflict{Section: "naming", Summary: section + " 在不同原文分段中不一致", EvidenceRefs: refs})
	} else {
		merged.Warnings = appendUniqueStrings(merged.Warnings, []string{section + " 在不同原文分段中不一致且缺少足够证据引用，已省略。"}, 128)
	}
	return rules.NamingConstraint{}
}

func namingConstraintEmpty(value rules.NamingConstraint) bool {
	return !value.Required && strings.TrimSpace(value.Pattern) == "" && strings.TrimSpace(value.Template) == "" && value.MaxLength == 0
}

func namingConstraintsEquivalent(left, right rules.NamingConstraint) bool {
	return left.Required == right.Required && left.Pattern == right.Pattern && left.Template == right.Template && left.MaxLength == right.MaxLength
}

func namingProfilesEquivalent(left, right rules.NamingProfile) bool {
	left.ReleaseTitle.EvidenceRefs, right.ReleaseTitle.EvidenceRefs = nil, nil
	left.ResourceClasses, right.ResourceClasses = nil, nil
	left.CategoryIDs, right.CategoryIDs = nil, nil
	leftBody, _ := json.Marshal(left)
	rightBody, _ := json.Marshal(right)
	return string(leftBody) == string(rightBody)
}

func appendUniqueStrings(current, incoming []string, limit int) []string {
	seen := make(map[string]bool, len(current)+len(incoming))
	result := make([]string, 0, min(limit, len(current)+len(incoming)))
	for _, values := range [][]string{current, incoming} {
		for _, value := range values {
			value = strings.TrimSpace(value)
			if value == "" || seen[value] || len(result) >= limit {
				continue
			}
			seen[value] = true
			result = append(result, value)
		}
	}
	return result
}

func appendUniqueInts(current, incoming []int, limit int) []int {
	seen := map[int]bool{}
	result := make([]int, 0, min(limit, len(current)+len(incoming)))
	for _, values := range [][]int{current, incoming} {
		for _, value := range values {
			if value <= 0 || seen[value] || len(result) >= limit {
				continue
			}
			seen[value] = true
			result = append(result, value)
		}
	}
	return result
}

func appendUniqueJSON[T any](current, incoming []T, limit int) []T {
	seen := map[string]bool{}
	result := make([]T, 0, min(limit, len(current)+len(incoming)))
	for _, values := range [][]T{current, incoming} {
		for _, value := range values {
			body, _ := json.Marshal(value)
			key := string(body)
			if seen[key] || len(result) >= limit {
				continue
			}
			seen[key] = true
			result = append(result, value)
		}
	}
	return result
}

func minPositive(current, incoming int) int {
	if current <= 0 {
		return incoming
	}
	if incoming <= 0 || current <= incoming {
		return current
	}
	return incoming
}

func maxPositive(current, incoming int) int {
	if incoming > current {
		return incoming
	}
	return current
}
