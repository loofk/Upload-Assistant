package nexusphp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"html"
	"io"
	"mime"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

const (
	maxDetailsBytes = 8 << 20
	maxTorrentBytes = 40 << 20
)

type RuntimeSiteProvider interface {
	GetRuntimeSite(context.Context, string) (integrations.RuntimeSite, error)
}

type Profile struct {
	SiteCode string
	BaseURL  string
}

var ProductionProfiles = []Profile{
	{SiteCode: "U2", BaseURL: "https://u2.dmhy.org"},
	{SiteCode: "CHD", BaseURL: "https://ptchdbits.co"},
}

type Adapter struct {
	profile    Profile
	baseURL    *url.URL
	provider   RuntimeSiteProvider
	accessGate sites.AccessGate
	httpClient *http.Client
}

func New(profile Profile, provider RuntimeSiteProvider, accessGate sites.AccessGate, client *http.Client) (*Adapter, error) {
	profile.SiteCode = strings.ToUpper(strings.TrimSpace(profile.SiteCode))
	parsed, err := url.Parse(strings.TrimRight(strings.TrimSpace(profile.BaseURL), "/"))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return nil, fmt.Errorf("invalid NexusPHP base URL")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("NexusPHP base URL must not contain credentials, query, or fragment")
	}
	if profile.SiteCode == "" || provider == nil || accessGate == nil {
		return nil, fmt.Errorf("NexusPHP site code, credential provider, and access gate are required")
	}
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	} else {
		clone := *client
		client = &clone
		if client.Timeout <= 0 {
			client.Timeout = 30 * time.Second
		}
	}
	client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &Adapter{profile: profile, baseURL: parsed, provider: provider, accessGate: accessGate, httpClient: client}, nil
}

func (adapter *Adapter) SiteCode() string { return adapter.profile.SiteCode }

func (adapter *Adapter) Inspect(ctx context.Context, reference sites.SourceReference) (sites.SourceInfo, error) {
	return sites.WithAccess(ctx, adapter.accessGate, sites.AccessRequest{
		SiteCode: adapter.profile.SiteCode, Operation: "source.inspect", Class: sites.AccessGeneral,
	}, func(access *sites.AccessResult) (sites.SourceInfo, error) {
		return adapter.inspect(ctx, reference, access)
	})
}

func (adapter *Adapter) inspect(ctx context.Context, reference sites.SourceReference, access *sites.AccessResult) (sites.SourceInfo, error) {
	if err := adapter.validateReference(reference); err != nil {
		return sites.SourceInfo{}, err
	}
	runtime, err := adapter.runtime(ctx)
	if err != nil {
		return sites.SourceInfo{}, err
	}
	cookie := strings.TrimSpace(runtime.Credentials["cookie"])
	if cookie == "" {
		return sites.SourceInfo{}, sites.NewAdapterError(
			"site_cookie_required", "an enabled cookie credential is required to inspect the source torrent", false, nil,
		)
	}
	detailsURL := adapter.resolve("/details.php", url.Values{"id": []string{reference.TorrentID}})
	response, err := adapter.get(ctx, detailsURL, cookie)
	if err != nil {
		return sites.SourceInfo{}, err
	}
	defer response.Body.Close()
	access.StatusCode = response.StatusCode
	if err := validateStatus(response, "source details"); err != nil {
		return sites.SourceInfo{}, err
	}
	body, err := readBounded(response.Body, maxDetailsBytes)
	if err != nil {
		return sites.SourceInfo{}, sites.NewAdapterError("source_details_invalid", "source details response is too large or unreadable", false, err)
	}
	digest := sha256.Sum256(body)
	access.ResponseSHA256 = hex.EncodeToString(digest[:])
	if !looksLikeHTML(response.Header.Get("Content-Type"), body) {
		return sites.SourceInfo{}, sites.NewAdapterError("source_details_invalid", "source details response is not HTML", false, nil)
	}
	page := string(body)
	if looksLikeLogin(page) {
		return sites.SourceInfo{}, sites.NewAdapterError("site_authentication_failed", "source site returned a login page; refresh the cookie credential", false, nil)
	}
	return parseDetails(adapter.profile.SiteCode, reference.TorrentID, detailsURL.String(), page), nil
}

func (adapter *Adapter) Download(ctx context.Context, reference sites.SourceReference) (sites.DownloadedTorrent, error) {
	return sites.WithAccess(ctx, adapter.accessGate, sites.AccessRequest{
		SiteCode: adapter.profile.SiteCode, Operation: "source.torrent_download", Class: sites.AccessGeneral,
	}, func(access *sites.AccessResult) (sites.DownloadedTorrent, error) {
		return adapter.download(ctx, reference, access)
	})
}

func (adapter *Adapter) download(ctx context.Context, reference sites.SourceReference, access *sites.AccessResult) (sites.DownloadedTorrent, error) {
	if err := adapter.validateReference(reference); err != nil {
		return sites.DownloadedTorrent{}, err
	}
	runtime, err := adapter.runtime(ctx)
	if err != nil {
		return sites.DownloadedTorrent{}, err
	}
	passkey := strings.TrimSpace(runtime.Credentials["passkey"])
	if passkey == "" {
		return sites.DownloadedTorrent{}, sites.NewAdapterError(
			"site_passkey_required", "an enabled passkey credential is required to download the source torrent", false, nil,
		)
	}
	downloadURL := adapter.resolve("/download.php", url.Values{
		"id": []string{reference.TorrentID}, "passkey": []string{passkey},
	})
	response, err := adapter.get(ctx, downloadURL, runtime.Credentials["cookie"])
	if err != nil {
		return sites.DownloadedTorrent{}, err
	}
	defer response.Body.Close()
	access.StatusCode = response.StatusCode
	if err := validateStatus(response, "source torrent"); err != nil {
		return sites.DownloadedTorrent{}, err
	}
	body, err := readBounded(response.Body, maxTorrentBytes)
	if err != nil {
		return sites.DownloadedTorrent{}, sites.NewAdapterError("source_torrent_invalid", "source torrent response is too large or unreadable", false, err)
	}
	digest := sha256.Sum256(body)
	access.ResponseSHA256 = hex.EncodeToString(digest[:])
	if looksLikeHTML(response.Header.Get("Content-Type"), body) || looksLikeLogin(string(body)) {
		return sites.DownloadedTorrent{}, sites.NewAdapterError("site_authentication_failed", "source site did not return a torrent; refresh cookie/passkey credentials", false, nil)
	}
	hashes, err := torrentmeta.Hashes(body)
	if err != nil {
		return sites.DownloadedTorrent{}, sites.NewAdapterError("source_torrent_invalid", "source site returned invalid torrent metainfo", false, err)
	}
	// A tracker-controlled Content-Disposition filename is not evidence and can
	// contain account-specific material. Persist a deterministic local name.
	filename := adapter.profile.SiteCode + "-" + reference.TorrentID + ".torrent"
	return sites.DownloadedTorrent{
		Bytes: body, Filename: filename, ContentType: "application/x-bittorrent",
		SizeBytes: int64(len(body)), SHA256: hex.EncodeToString(digest[:]), Hashes: hashes,
	}, nil
}

func (adapter *Adapter) ListCandidates(ctx context.Context, request sites.CandidateScanRequest) (sites.CandidateScanEvidence, error) {
	return sites.WithAccess(ctx, adapter.accessGate, sites.AccessRequest{
		SiteCode: adapter.profile.SiteCode, Operation: "source.candidates", Class: sites.AccessSearch,
	}, func(access *sites.AccessResult) (sites.CandidateScanEvidence, error) {
		return adapter.listCandidates(ctx, request, access)
	})
}

func (adapter *Adapter) listCandidates(ctx context.Context, request sites.CandidateScanRequest, access *sites.AccessResult) (sites.CandidateScanEvidence, error) {
	if request.Limit <= 0 {
		request.Limit = 20
	}
	if request.Limit > 100 {
		request.Limit = 100
	}
	if request.Page <= 0 {
		request.Page = 1
	}
	runtime, err := adapter.runtime(ctx)
	if err != nil {
		return sites.CandidateScanEvidence{}, err
	}
	cookie := strings.TrimSpace(runtime.Credentials["cookie"])
	if cookie == "" {
		return sites.CandidateScanEvidence{}, sites.NewAdapterError(
			"site_cookie_required", "an enabled cookie credential is required to list source candidates", false, nil,
		)
	}
	listingURL := adapter.resolve("/torrents.php", url.Values{"page": []string{strconv.Itoa(request.Page - 1)}})
	response, err := adapter.get(ctx, listingURL, cookie)
	if err != nil {
		return sites.CandidateScanEvidence{}, err
	}
	defer response.Body.Close()
	access.StatusCode = response.StatusCode
	if err := validateStatus(response, "source candidate listing"); err != nil {
		return sites.CandidateScanEvidence{}, err
	}
	body, err := readBounded(response.Body, maxDetailsBytes)
	if err != nil || !looksLikeHTML(response.Header.Get("Content-Type"), body) {
		return sites.CandidateScanEvidence{}, sites.NewAdapterError("source_candidate_listing_invalid", "source candidate listing is unreadable or not HTML", false, err)
	}
	digest := sha256.Sum256(body)
	access.ResponseSHA256 = hex.EncodeToString(digest[:])
	if looksLikeLogin(string(body)) {
		return sites.CandidateScanEvidence{}, sites.NewAdapterError("site_authentication_failed", "source site returned a login page; refresh the cookie credential", false, nil)
	}
	items := parseCandidateRows(adapter.profile.SiteCode, adapter.baseURL, string(body), request.Limit)
	downloadable := strings.TrimSpace(runtime.Credentials["passkey"]) != ""
	for index := range items {
		items[index].Downloadable = downloadable
		if !downloadable {
			items[index].DownloadBlockers = []string{"site_passkey_required"}
		}
	}
	return sites.CandidateScanEvidence{
		SiteCode: adapter.profile.SiteCode, Page: request.Page, Limit: request.Limit,
		Items: items, ScannedAt: time.Now().UTC(),
	}, nil
}

func (adapter *Adapter) runtime(ctx context.Context) (integrations.RuntimeSite, error) {
	runtime, err := adapter.provider.GetRuntimeSite(ctx, adapter.profile.SiteCode)
	if err != nil {
		return integrations.RuntimeSite{}, sites.NewAdapterError("site_configuration_unavailable", "source site configuration is unavailable or disabled", false, err)
	}
	if runtime.Adapter != "nexusphp" {
		return integrations.RuntimeSite{}, sites.NewAdapterError("site_adapter_mismatch", "configured source site adapter is not NexusPHP", false, nil)
	}
	return runtime, nil
}

func (adapter *Adapter) validateReference(reference sites.SourceReference) error {
	if !strings.EqualFold(reference.Tracker, adapter.profile.SiteCode) {
		return sites.NewAdapterError("source_reference_mismatch", "source reference does not belong to this site adapter", false, nil)
	}
	if reference.TorrentID == "" {
		return sites.NewAdapterError("source_reference_invalid", "source torrent id is required", false, nil)
	}
	return nil
}

func (adapter *Adapter) resolve(path string, query url.Values) *url.URL {
	result := *adapter.baseURL
	result.Path = strings.TrimRight(adapter.baseURL.Path, "/") + path
	result.RawQuery = query.Encode()
	return &result
}

func (adapter *Adapter) get(ctx context.Context, target *url.URL, cookieSecret string) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return nil, sites.NewAdapterError("source_request_failed", "could not build source site request", false, err)
	}
	request.Header.Set("Accept", "text/html,application/x-bittorrent,application/octet-stream;q=0.9")
	request.Header.Set("User-Agent", "Upload-Assistant-Go/2")
	for _, cookie := range parseCookieSecret(cookieSecret) {
		request.AddCookie(cookie)
	}
	response, err := adapter.httpClient.Do(request)
	if err != nil {
		// Do not wrap url.Error text: download URLs can contain a passkey.
		return nil, sites.NewAdapterError("source_request_failed", "source site request failed", true, nil)
	}
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		_ = response.Body.Close()
		return nil, sites.NewAdapterError("site_authentication_failed", "source site redirected the request; refresh credentials and verify the site endpoint", false, nil)
	}
	return response, nil
}

func validateStatus(response *http.Response, resource string) error {
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return sites.NewAdapterError("site_authentication_failed", resource+" request was rejected by the source site", false, nil)
	}
	if response.StatusCode == http.StatusNotFound {
		return sites.NewAdapterError("source_torrent_not_found", resource+" was not found", false, nil)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return sites.NewAdapterError("source_site_unavailable", fmt.Sprintf("%s request returned HTTP %d", resource, response.StatusCode), response.StatusCode >= 500, nil)
	}
	return nil
}

func readBounded(reader io.Reader, limit int64) ([]byte, error) {
	body, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("response exceeds %d bytes", limit)
	}
	return body, nil
}

func parseCookieSecret(secret string) []*http.Cookie {
	values := map[string]string{}
	for _, rawLine := range strings.Split(secret, "\n") {
		line := strings.TrimSpace(strings.TrimSuffix(rawLine, "\r"))
		if strings.HasPrefix(line, "#HttpOnly_") {
			line = strings.TrimPrefix(line, "#HttpOnly_")
		} else if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 7 {
			values[fields[5]] = fields[6]
			continue
		}
		for _, pair := range strings.Split(line, ";") {
			name, value, found := strings.Cut(pair, "=")
			name, value = strings.TrimSpace(name), strings.TrimSpace(value)
			if found && validCookieName(name) {
				values[name] = value
			}
		}
	}
	names := make([]string, 0, len(values))
	for name := range values {
		if validCookieName(name) {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	result := make([]*http.Cookie, 0, len(names))
	for _, name := range names {
		result = append(result, &http.Cookie{Name: name, Value: values[name]})
	}
	return result
}

var cookieNamePattern = regexp.MustCompile(`^[!#$%&'*+\-.^_` + "`" + `|~0-9A-Za-z]+$`)

func validCookieName(name string) bool { return cookieNamePattern.MatchString(name) }

var (
	tagPattern         = regexp.MustCompile(`(?is)<[^>]+>`)
	spacePattern       = regexp.MustCompile(`\s+`)
	metaTitlePattern   = regexp.MustCompile(`(?is)<meta[^>]+(?:property|name)=["'](?:og:title|title)["'][^>]+content=["']([^"']+)["']`)
	metaTitleReverse   = regexp.MustCompile(`(?is)<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["'](?:og:title|title)["']`)
	headingPattern     = regexp.MustCompile(`(?is)<h[12][^>]*>(.*?)</h[12]>`)
	titlePattern       = regexp.MustCompile(`(?is)<title[^>]*>(.*?)</title>`)
	descriptionPattern = regexp.MustCompile(`(?is)<(?:div|td)[^>]+(?:id|class)=["'][^"']*(?:kdescr|torrent-description|description)[^"']*["'][^>]*>(.*?)</(?:div|td)>`)
	imdbPattern        = regexp.MustCompile(`(?i)(?:imdb\.com/title/)?tt(\d{5,12})`)
	tmdbPattern        = regexp.MustCompile(`(?i)themoviedb\.org/(movie|tv)/(\d+)`)
	doubanPattern      = regexp.MustCompile(`(?i)(?:movie\.)?douban\.com/subject/(\d+)`)
	anidbPattern       = regexp.MustCompile(`(?i)anidb\.net/(?:anime/|a)(\d+)`)
	hashPattern        = regexp.MustCompile(`(?i)(?:info[ _-]?hash|torrent[ _-]?hash)[^a-f0-9]{0,40}([a-f0-9]{40})`)
	loginFormPattern   = regexp.MustCompile(`(?is)<form[^>]+(?:login\.php|name=["']login)`)
	rowPattern         = regexp.MustCompile(`(?is)(<tr\b[^>]*>.*?</tr>)`)
	detailsLinkPattern = regexp.MustCompile(`(?is)<a\b[^>]*href=["']([^"']*details\.php\?[^"']*)["'][^>]*>(.*?)</a>`)
	dataBytesPattern   = regexp.MustCompile(`(?i)data-(?:size-)?bytes=["'](\d+)["']`)
	humanSizePattern   = regexp.MustCompile(`(?i)\b(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB)\b`)
	timeElementPattern = regexp.MustCompile(`(?is)<time\b[^>]*datetime=["']([^"']+)["']`)
	dateTimePattern    = regexp.MustCompile(`\b(20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\b`)
)

func parseCandidateRows(siteCode string, baseURL *url.URL, page string, limit int) []sites.SourceCandidate {
	rows := rowPattern.FindAllStringSubmatch(page, -1)
	items := make([]sites.SourceCandidate, 0, min(limit, len(rows)))
	seen := make(map[string]struct{})
	for _, rowMatch := range rows {
		row := rowMatch[1]
		link := detailsLinkPattern.FindStringSubmatch(row)
		if len(link) != 3 {
			continue
		}
		href := html.UnescapeString(strings.TrimSpace(link[1]))
		parsed, err := url.Parse(href)
		if err != nil {
			continue
		}
		torrentID := strings.TrimSpace(parsed.Query().Get("id"))
		if torrentID == "" {
			continue
		}
		if _, exists := seen[torrentID]; exists {
			continue
		}
		seen[torrentID] = struct{}{}
		details := *baseURL
		details.Path = strings.TrimRight(baseURL.Path, "/") + "/details.php"
		details.RawQuery = url.Values{"id": []string{torrentID}}.Encode()
		labels := promotionLabels(normalizedText(row))
		item := sites.SourceCandidate{
			Tracker: siteCode, TorrentID: torrentID, DetailsURL: details.String(),
			Title: normalizedText(link[2]), SizeBytes: candidateSize(row),
			PublishedAt: candidateTime(row), PromotionLabels: labels, DownloadBlockers: []string{},
		}
		for _, label := range labels {
			if label == "free" || label == "freeleech" || label == "免费" || label == "免費" {
				item.Free = true
			}
		}
		items = append(items, item)
		if len(items) == limit {
			break
		}
	}
	return items
}

func candidateSize(row string) int64 {
	if match := dataBytesPattern.FindStringSubmatch(row); len(match) == 2 {
		value, _ := strconv.ParseInt(match[1], 10, 64)
		return value
	}
	match := humanSizePattern.FindStringSubmatch(normalizedText(row))
	if len(match) != 3 {
		return 0
	}
	value, err := strconv.ParseFloat(match[1], 64)
	if err != nil {
		return 0
	}
	multipliers := map[string]float64{
		"KIB": 1 << 10, "MIB": 1 << 20, "GIB": 1 << 30, "TIB": 1 << 40,
		"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
	}
	return int64(value * multipliers[strings.ToUpper(match[2])])
}

func candidateTime(row string) *time.Time {
	value := ""
	if match := timeElementPattern.FindStringSubmatch(row); len(match) == 2 {
		value = strings.TrimSpace(match[1])
	} else if match := dateTimePattern.FindStringSubmatch(normalizedText(row)); len(match) == 2 {
		value = strings.TrimSpace(match[1])
	}
	for _, layout := range []string{time.RFC3339, "2006-01-02 15:04:05", "2006-01-02 15:04", "2006-01-02T15:04:05"} {
		var parsed time.Time
		var err error
		if layout == time.RFC3339 {
			parsed, err = time.Parse(layout, value)
		} else {
			parsed, err = time.ParseInLocation(layout, value, time.FixedZone("Asia/Shanghai", 8*60*60))
		}
		if err == nil {
			parsed = parsed.UTC()
			return &parsed
		}
	}
	return nil
}

func parseDetails(siteCode, torrentID, detailsURL, page string) sites.SourceInfo {
	text := normalizedText(page)
	result := sites.SourceInfo{
		Tracker: siteCode, TorrentID: torrentID, DetailsURL: detailsURL,
		Name:            firstCapture(page, metaTitlePattern, metaTitleReverse, headingPattern, titlePattern),
		PromotionLabels: []string{}, RetrievedAt: time.Now().UTC(),
	}
	if match := imdbPattern.FindStringSubmatch(page); len(match) == 2 {
		result.IMDbID = "tt" + match[1]
	}
	if match := tmdbPattern.FindStringSubmatch(page); len(match) == 3 {
		result.TMDbType, result.TMDbID = strings.ToLower(match[1]), match[2]
	}
	if match := doubanPattern.FindStringSubmatch(page); len(match) == 2 {
		result.DoubanID = match[1]
		result.DoubanURL = "https://movie.douban.com/subject/" + match[1] + "/"
	}
	if match := anidbPattern.FindStringSubmatch(page); len(match) == 2 {
		result.AniDBID = match[1]
	}
	if match := hashPattern.FindStringSubmatch(page); len(match) == 2 {
		result.TorrentHash = strings.ToLower(match[1])
	}
	if match := descriptionPattern.FindStringSubmatch(page); len(match) == 2 {
		result.DescriptionHTML = strings.TrimSpace(match[1])
		result.DescriptionLength = len([]rune(normalizedText(result.DescriptionHTML)))
	}
	for _, label := range promotionLabels(text) {
		result.PromotionLabels = append(result.PromotionLabels, label)
		if label == "free" || label == "freeleech" || label == "免费" || label == "免費" {
			result.Free = true
		}
	}
	return result
}

func firstCapture(page string, patterns ...*regexp.Regexp) string {
	for _, pattern := range patterns {
		if match := pattern.FindStringSubmatch(page); len(match) == 2 {
			value := normalizedText(match[1])
			if value != "" {
				return value
			}
		}
	}
	return ""
}

func normalizedText(value string) string {
	return strings.TrimSpace(spacePattern.ReplaceAllString(html.UnescapeString(tagPattern.ReplaceAllString(value, " ")), " "))
}

func promotionLabels(text string) []string {
	lower := strings.ToLower(text)
	labels := make([]string, 0, 4)
	for _, label := range []string{"freeleech", "free", "免费", "免費", "2x上传", "2x上傳", "50%下载", "50%下載"} {
		if strings.Contains(lower, strings.ToLower(label)) {
			labels = append(labels, label)
		}
	}
	return labels
}

func looksLikeLogin(page string) bool {
	lower := strings.ToLower(page)
	return loginFormPattern.MatchString(page) ||
		(strings.Contains(lower, "name=\"username\"") && strings.Contains(lower, "name=\"password\"")) ||
		strings.Contains(lower, "please login to continue")
}

func looksLikeHTML(contentType string, body []byte) bool {
	mediaType, _, _ := mime.ParseMediaType(contentType)
	if mediaType == "text/html" || mediaType == "application/xhtml+xml" {
		return true
	}
	trimmed := strings.TrimSpace(string(body))
	return strings.HasPrefix(strings.ToLower(trimmed), "<!doctype html") || strings.HasPrefix(strings.ToLower(trimmed), "<html")
}

func responseFilename(disposition string) string {
	_, parameters, err := mime.ParseMediaType(disposition)
	if err != nil {
		return ""
	}
	filename := strings.TrimSpace(parameters["filename"])
	if filename == "" || filename == "." || filename == ".." || strings.ContainsAny(filename, `/\\`) {
		return ""
	}
	return filename
}

var (
	_ sites.SourceAdapter          = (*Adapter)(nil)
	_ sites.SourceCandidateAdapter = (*Adapter)(nil)
)
