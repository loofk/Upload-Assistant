package sites

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

var pathTorrentIDPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)/details/(\d+)`),
	regexp.MustCompile(`(?i)/detail/(\d+)`),
	regexp.MustCompile(`(?i)/torrent/(\d+)`),
	regexp.MustCompile(`(?i)/torrents/(\d+)`),
	regexp.MustCompile(`(?i)/download/(\d+)`),
	regexp.MustCompile(`(?i)/dl/(\d+)`),
}

var trackerHosts = map[string]string{
	"audiences.me":  "AUDIENCES",
	"ptchdbits.co":  "CHD",
	"hd-space.org":  "HDS",
	"hdsky.me":      "HDSKY",
	"hhanclub.net":  "HHAN",
	"m-team.cc":     "MTEAM",
	"kp.m-team.cc":  "MTEAM",
	"pt.m-team.cc":  "MTEAM",
	"api.m-team.cc": "MTEAM",
	"ourbits.club":  "OB",
	"pterclub.com":  "PTER",
	"tjupt.org":     "TJUPT",
	"totheglory.im": "TTG",
	"u2.dmhy.org":   "U2",
}

type SourceReference struct {
	Tracker         string `json:"tracker"`
	TorrentID       string `json:"torrent_id"`
	RequestedSource string `json:"requested_source"`
	DetailsURL      string `json:"details_url"`
	InferredTracker bool   `json:"inferred_tracker"`
}

func ParseSourceReference(value string) (SourceReference, error) {
	raw := strings.TrimSpace(value)
	if raw == "" {
		return SourceReference{}, fmt.Errorf("source URL is required")
	}
	parseValue := raw
	if !strings.Contains(parseValue, "://") {
		parseValue = "https://" + parseValue
	}
	parsed, err := url.Parse(parseValue)
	if err != nil || parsed.Hostname() == "" {
		return SourceReference{}, fmt.Errorf("source URL is invalid")
	}
	host := strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
	host = strings.TrimPrefix(host, "www.")
	tracker := ""
	for knownHost, candidate := range trackerHosts {
		if host == knownHost || strings.HasSuffix(host, "."+knownHost) {
			tracker = candidate
			break
		}
	}
	if tracker == "" {
		return SourceReference{}, fmt.Errorf("unsupported source tracker host: %s", host)
	}
	torrentID := extractTorrentID(parsed)
	if torrentID == "" {
		return SourceReference{}, fmt.Errorf("could not extract torrent id from source URL")
	}
	return SourceReference{
		Tracker: tracker, TorrentID: torrentID, RequestedSource: raw,
		DetailsURL: parsed.String(), InferredTracker: true,
	}, nil
}

func extractTorrentID(parsed *url.URL) string {
	for _, key := range []string{"id", "torrentid", "torrent_id", "tid"} {
		value := parsed.Query().Get(key)
		if digitsOnly(value) {
			return value
		}
	}
	for _, pattern := range pathTorrentIDPatterns {
		match := pattern.FindStringSubmatch(parsed.EscapedPath())
		if len(match) == 2 {
			return match[1]
		}
	}
	return ""
}

func digitsOnly(value string) bool {
	if value == "" {
		return false
	}
	for _, character := range value {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}
