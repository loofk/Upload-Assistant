package rules

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

const DefaultUploadSafetyMargin = "20MB/s"

var byteRatePattern = regexp.MustCompile(`(?i)^([0-9]+(?:\.[0-9]+)?)\s*(B|K|KB|KIB|M|MB|MIB|G|GB|GIB)\s*/\s*S$`)

// ParseByteRate converts human-maintained Markdown policy values such as
// "20MiB/s" or "100M/s" into qBittorrent's bytes-per-second unit. Empty
// values mean no configured cap. Ambiguous or malformed values fail closed.
func ParseByteRate(value string) (int64, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, nil
	}
	match := byteRatePattern.FindStringSubmatch(value)
	if len(match) != 3 {
		return 0, fmt.Errorf("invalid byte rate %q", value)
	}
	number, err := strconv.ParseFloat(match[1], 64)
	if err != nil || number <= 0 {
		return 0, fmt.Errorf("invalid byte rate %q", value)
	}
	unit := strings.ToUpper(match[2])
	multiplier := float64(1)
	switch unit {
	case "K", "KB":
		multiplier = 1_000
	case "KIB":
		multiplier = 1 << 10
	case "M", "MB":
		multiplier = 1_000_000
	case "MIB":
		multiplier = 1 << 20
	case "G", "GB":
		multiplier = 1_000_000_000
	case "GIB":
		multiplier = 1 << 30
	}
	result := number * multiplier
	if result > math.MaxInt64 || result < 1 {
		return 0, fmt.Errorf("byte rate %q is out of range", value)
	}
	return int64(math.Round(result)), nil
}

// NewRateLimitPolicy normalizes one reviewed tracker limit. Upload callers pass
// DefaultUploadSafetyMargin; download callers pass an empty margin. When the
// declared value is not greater than the margin, Enforced remains empty so the
// review workspace fails closed and asks for an explicit value.
func NewRateLimitPolicy(declared, margin, scope string) (*RateLimitPolicy, error) {
	declared = strings.TrimSpace(declared)
	margin = strings.TrimSpace(margin)
	scope = strings.TrimSpace(scope)
	if declared == "" {
		return nil, nil
	}
	if scope == "" {
		scope = "unknown"
	}
	declaredBytes, err := ParseByteRate(declared)
	if err != nil {
		return nil, err
	}
	marginBytes, err := ParseByteRate(margin)
	if err != nil {
		return nil, err
	}
	policy := &RateLimitPolicy{Declared: declared, SafetyMargin: margin, Scope: scope}
	if scope == "per_torrent" && declaredBytes > marginBytes {
		policy.Enforced = FormatByteRate(declaredBytes - marginBytes)
	}
	return policy, nil
}

// FormatByteRate emits a stable exact byte-rate string. Prefer familiar
// decimal units when the value divides evenly; otherwise preserve exact bytes.
func FormatByteRate(value int64) string {
	if value <= 0 {
		return ""
	}
	for _, unit := range []struct {
		name  string
		value int64
	}{{"GB/s", 1_000_000_000}, {"MB/s", 1_000_000}, {"KB/s", 1_000}} {
		if value >= unit.value && value%unit.value == 0 {
			return fmt.Sprintf("%d%s", value/unit.value, unit.name)
		}
	}
	return fmt.Sprintf("%dB/s", value)
}
