package rules

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

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
