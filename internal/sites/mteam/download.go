package mteam

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxDownloadedTargetTorrentBytes = 32 << 20

var (
	_                    sites.TargetTorrentDownloadAdapter = (*Client)(nil)
	downloadHostPattern                                     = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$`)
	defaultDownloadHosts                                    = []string{"api.m-team.cc", "kp.m-team.cc", "tracker.m-team.cc"}
)

func (client *Client) DownloadUploadedTorrent(ctx context.Context, request sites.TargetTorrentDownloadRequest, actor workflow.Actor) (sites.DownloadedTargetTorrent, error) {
	if err := validateTargetTorrentDownloadRequest(request); err != nil {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("target_torrent_download_request_invalid", err.Error(), false, err)
	}
	runtime, err := client.store.GetRuntimeSite(ctx, "MTEAM")
	if err != nil {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("site_configuration_unavailable", "MTEAM site configuration is unavailable or disabled", false, err)
	}
	if runtime.Adapter != "mteam_api" {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("site_adapter_mismatch", "configured MTEAM site adapter is not mteam_api", false, nil)
	}
	apiKey := strings.TrimSpace(runtime.Credentials["api_key"])
	if apiKey == "" {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("site_api_key_required", "an enabled MTEAM api_key credential is required", false, nil)
	}
	config, endpoint, err := parseAPIConfig(runtime.Config)
	if err != nil {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("site_configuration_invalid", err.Error(), false, err)
	}
	configurationSHA := runtime.ConfigurationSHA256
	if configurationSHA == "" {
		digest := sha256.Sum256(runtime.Config)
		configurationSHA = hex.EncodeToString(digest[:])
	}
	audit := targetTorrentDownloadAudit(request, configurationSHA)
	if err := client.store.AuditSiteAction(ctx, "MTEAM", "target.torrent_download_intent", audit, actor); err != nil {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError(
			"target_torrent_download_audit_failed", "could not persist the MTEAM target torrent download intent; no request was attempted", false, err,
		)
	}

	tokenResponse, tokenSHA, signedURL, err := client.generateDownloadURL(ctx, endpoint, config, apiKey, request.TorrentID)
	if err != nil {
		client.auditTargetTorrentDownloadOutcome(ctx, request, configurationSHA, "failed", errorCode(err), "", actor)
		return sites.DownloadedTargetTorrent{}, err
	}
	_ = tokenResponse // The response is intentionally retained only by hash.
	if err := validateSignedDownloadURL(signedURL, endpoint, config.DownloadHosts); err != nil {
		client.auditTargetTorrentDownloadOutcome(ctx, request, configurationSHA, "rejected", "untrusted_download_url", tokenSHA, actor)
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError(
			"target_torrent_download_url_rejected", "MTEAM returned a download URL outside the configured trusted hosts", false, err,
		)
	}
	downloaded, contentType, err := client.fetchSignedTorrent(ctx, signedURL, config.TimeoutSeconds)
	if err != nil {
		client.auditTargetTorrentDownloadOutcome(ctx, request, configurationSHA, "failed", errorCode(err), tokenSHA, actor)
		return sites.DownloadedTargetTorrent{}, err
	}
	inspection, err := torrentmeta.Inspect(downloaded)
	if err != nil {
		client.auditTargetTorrentDownloadOutcome(ctx, request, configurationSHA, "rejected", "invalid_torrent", tokenSHA, actor)
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("target_torrent_download_invalid", "MTEAM returned invalid torrent metainfo", false, err)
	}
	if err := validateDownloadedMTeamTorrent(inspection, request.ContentFingerprintSHA256); err != nil {
		client.auditTargetTorrentDownloadOutcome(ctx, request, configurationSHA, "rejected", "payload_mismatch", tokenSHA, actor)
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError("target_torrent_payload_mismatch", err.Error(), false, err)
	}
	bodyDigest := sha256.Sum256(downloaded)
	announceDigest := sha256.Sum256([]byte(inspection.Announce))
	urlDigest := sha256.Sum256([]byte(signedURL.String()))
	evidence := sites.TargetTorrentDownloadEvidence{
		SiteCode: "MTEAM", Adapter: runtime.Adapter, ConfigurationSHA256: configurationSHA,
		TorrentID: request.TorrentID, Filename: "mteam-" + request.TorrentID + ".torrent", ContentType: contentType,
		SizeBytes: int64(len(downloaded)), SHA256: hex.EncodeToString(bodyDigest[:]), Hashes: inspection.Hashes,
		ContentFingerprint: inspection.ContentFingerprint, AnnounceSHA256: hex.EncodeToString(announceDigest[:]),
		TokenResponseSHA256: tokenSHA, SignedDownloadURLSHA256: hex.EncodeToString(urlDigest[:]), DownloadedAt: time.Now().UTC(),
	}
	resultAudit := targetTorrentDownloadAudit(request, configurationSHA)
	resultAudit["torrent_sha256"] = evidence.SHA256
	resultAudit["v1_infohash"] = evidence.Hashes.V1SHA1
	resultAudit["v2_infohash"] = evidence.Hashes.V2SHA256
	resultAudit["size_bytes"] = evidence.SizeBytes
	resultAudit["announce_sha256"] = evidence.AnnounceSHA256
	resultAudit["token_response_sha256"] = evidence.TokenResponseSHA256
	resultAudit["signed_download_url_sha256"] = evidence.SignedDownloadURLSHA256
	if err := client.store.AuditSiteAction(ctx, "MTEAM", "target.torrent_download_result", resultAudit, actor); err != nil {
		return sites.DownloadedTargetTorrent{}, sites.NewAdapterError(
			"target_torrent_download_audit_failed", "downloaded the MTEAM torrent but could not persist its result audit; retrying the download is safe", true, err,
		)
	}
	return sites.DownloadedTargetTorrent{Bytes: downloaded, Evidence: evidence}, nil
}

func validateTargetTorrentDownloadRequest(request sites.TargetTorrentDownloadRequest) error {
	if request.JobID == "" || request.AttemptID == "" || !validSHA256(request.UploadReceiptSHA256) ||
		!validSHA256(request.SubmittedTorrentSHA256) || !validSHA256(request.ContentFingerprintSHA256) {
		return fmt.Errorf("target torrent download audit bindings are incomplete")
	}
	if len(request.TorrentID) == 0 || len(request.TorrentID) > 20 {
		return fmt.Errorf("target torrent id is invalid")
	}
	for _, character := range request.TorrentID {
		if character < '0' || character > '9' {
			return fmt.Errorf("target torrent id is invalid")
		}
	}
	return nil
}

func (client *Client) generateDownloadURL(ctx context.Context, endpoint *url.URL, config apiConfig, apiKey, torrentID string) ([]byte, string, *url.URL, error) {
	requestURL := resolveAPI(endpoint, "/api/torrent/genDlToken")
	body := url.Values{"id": []string{torrentID}}.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL.String(), strings.NewReader(body))
	if err != nil {
		return nil, "", nil, sites.NewAdapterError("target_torrent_download_request_invalid", "could not build the MTEAM token request", false, err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	request.Header.Set("User-Agent", "Upload-Assistant-Go/2")
	request.Header.Set("x-api-key", apiKey)
	httpClient := *client.httpClient
	httpClient.Timeout = time.Duration(config.TimeoutSeconds) * time.Second
	response, err := httpClient.Do(request)
	if err != nil {
		return nil, "", nil, sites.NewAdapterError("target_torrent_download_failed", "MTEAM download-token request failed", true, nil)
	}
	defer response.Body.Close()
	responseBody, readErr := io.ReadAll(io.LimitReader(response.Body, maxAPIResponse+1))
	if readErr != nil || len(responseBody) > maxAPIResponse {
		return nil, "", nil, sites.NewAdapterError("target_torrent_download_response_invalid", "MTEAM download-token response is unreadable or too large", false, readErr)
	}
	digest := sha256.Sum256(responseBody)
	responseSHA := hex.EncodeToString(digest[:])
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return responseBody, responseSHA, nil, sites.NewAdapterError("site_authentication_failed", "MTEAM rejected the API key", false, nil)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return responseBody, responseSHA, nil, sites.NewAdapterError(
			"target_torrent_download_failed", fmt.Sprintf("MTEAM download-token request returned HTTP %d", response.StatusCode), response.StatusCode >= 500, nil,
		)
	}
	signedURL, err := parseDownloadURLResponse(responseBody)
	if err != nil {
		return responseBody, responseSHA, nil, sites.NewAdapterError("target_torrent_download_response_invalid", "MTEAM did not return a valid signed torrent URL", false, err)
	}
	return responseBody, responseSHA, signedURL, nil
}

func (client *Client) fetchSignedTorrent(ctx context.Context, signedURL *url.URL, timeoutSeconds int) ([]byte, string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, signedURL.String(), nil)
	if err != nil {
		return nil, "", sites.NewAdapterError("target_torrent_download_url_rejected", "could not build the signed MTEAM download request", false, err)
	}
	request.Header.Set("Accept", "application/x-bittorrent, application/octet-stream;q=0.9")
	request.Header.Set("User-Agent", "Upload-Assistant-Go/2")
	httpClient := *client.httpClient
	httpClient.Timeout = time.Duration(timeoutSeconds) * time.Second
	response, err := httpClient.Do(request)
	if err != nil {
		return nil, "", sites.NewAdapterError("target_torrent_download_failed", "MTEAM signed torrent download failed", true, nil)
	}
	defer response.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(response.Body, maxDownloadedTargetTorrentBytes+1))
	if readErr != nil || len(body) > maxDownloadedTargetTorrentBytes {
		return nil, "", sites.NewAdapterError("target_torrent_download_invalid", "MTEAM torrent response is unreadable or too large", false, readErr)
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return nil, "", sites.NewAdapterError("site_authentication_failed", "MTEAM rejected the signed torrent download", false, nil)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, "", sites.NewAdapterError(
			"target_torrent_download_failed", fmt.Sprintf("MTEAM signed torrent download returned HTTP %d", response.StatusCode), response.StatusCode >= 500, nil,
		)
	}
	contentType := strings.TrimSpace(response.Header.Get("Content-Type"))
	if parsed, _, err := mime.ParseMediaType(contentType); err == nil {
		contentType = parsed
	}
	if contentType == "" {
		contentType = "application/x-bittorrent"
	}
	return body, contentType, nil
}

func parseDownloadURLResponse(body []byte) (*url.URL, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var response struct {
		Code any             `json:"code"`
		Data json.RawMessage `json:"data"`
	}
	if err := decoder.Decode(&response); err != nil || !successCode(response.Code) || len(response.Data) == 0 {
		return nil, fmt.Errorf("unsuccessful API envelope")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("trailing API response value")
	}
	var value string
	if err := json.Unmarshal(response.Data, &value); err != nil || strings.TrimSpace(value) == "" || len(value) > 16_384 {
		return nil, fmt.Errorf("download URL is missing or invalid")
	}
	return url.Parse(strings.TrimSpace(value))
}

func validateDownloadHosts(hosts []string) error {
	if len(hosts) > 32 {
		return fmt.Errorf("MTEAM download_hosts must contain at most 32 hostnames")
	}
	for _, host := range hosts {
		normalized := strings.ToLower(strings.TrimSpace(host))
		if normalized == "" || len(normalized) > 253 || (!downloadHostPattern.MatchString(normalized) && net.ParseIP(normalized) == nil) {
			return fmt.Errorf("MTEAM download_hosts contains an invalid hostname")
		}
	}
	return nil
}

func validateSignedDownloadURL(value, endpoint *url.URL, configured []string) error {
	if value == nil || value.Host == "" || value.User != nil || value.Fragment != "" || (value.Scheme != "https" && value.Scheme != "http") {
		return fmt.Errorf("signed URL structure is invalid")
	}
	hostname := strings.ToLower(value.Hostname())
	loopback := hostname == "localhost" || net.ParseIP(hostname) != nil && net.ParseIP(hostname).IsLoopback()
	if value.Scheme != "https" && !loopback {
		return fmt.Errorf("signed URL must use HTTPS")
	}
	allowed := append([]string(nil), defaultDownloadHosts...)
	allowed = append(allowed, strings.ToLower(endpoint.Hostname()))
	for _, host := range configured {
		allowed = append(allowed, strings.ToLower(strings.TrimSpace(host)))
	}
	if !slices.Contains(allowed, hostname) {
		return fmt.Errorf("signed URL host is not trusted")
	}
	return nil
}

func validateDownloadedMTeamTorrent(inspection torrentmeta.Inspection, expectedFingerprint string) error {
	if inspection.ContentFingerprint != expectedFingerprint {
		return fmt.Errorf("downloaded MTEAM torrent payload does not match the submitted torrent")
	}
	if !inspection.PrivateSet || !inspection.Private || inspection.Source != "MTEAM" || inspection.Announce == "" || len(inspection.ExtraInfoKeys) != 0 {
		return fmt.Errorf("downloaded MTEAM torrent does not satisfy the private target profile")
	}
	announce, err := url.Parse(inspection.Announce)
	if err != nil || announce.Host == "" || announce.User != nil || announce.Fragment != "" || (announce.Scheme != "https" && announce.Scheme != "http") {
		return fmt.Errorf("downloaded MTEAM torrent announce URL is invalid")
	}
	return nil
}

func targetTorrentDownloadAudit(request sites.TargetTorrentDownloadRequest, configurationSHA string) map[string]any {
	return map[string]any{
		"job_id": request.JobID, "attempt_id": request.AttemptID, "torrent_id": request.TorrentID,
		"upload_receipt_sha256": request.UploadReceiptSHA256, "submitted_torrent_sha256": request.SubmittedTorrentSHA256,
		"content_fingerprint_sha256": request.ContentFingerprintSHA256, "configuration_sha256": configurationSHA,
	}
}

func (client *Client) auditTargetTorrentDownloadOutcome(ctx context.Context, request sites.TargetTorrentDownloadRequest, configurationSHA, status, reason, tokenSHA string, actor workflow.Actor) {
	details := targetTorrentDownloadAudit(request, configurationSHA)
	details["status"], details["reason"] = status, reason
	if tokenSHA != "" {
		details["token_response_sha256"] = tokenSHA
	}
	_ = client.store.AuditSiteAction(ctx, "MTEAM", "target.torrent_download_outcome", details, actor)
}

func errorCode(err error) string {
	code, _, _ := sites.ErrorDetails(err)
	return code
}
