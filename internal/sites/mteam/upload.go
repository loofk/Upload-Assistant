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
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const (
	maxUploadDescriptionBytes = 4 << 20
	maxUploadMediaInfoBytes   = 16 << 20
	maxUploadTorrentBytes     = 32 << 20
	maxUploadRequestBytes     = 64 << 20
)

var _ sites.TargetUploadAdapter = (*Client)(nil)

func (client *Client) Upload(ctx context.Context, request sites.TargetUploadRequest, actor workflow.Actor) (sites.TargetUploadEvidence, error) {
	return sites.WithAccess(ctx, client.accessGate, sites.AccessRequest{
		SiteCode: "MTEAM", Operation: "target.upload", Class: sites.AccessGeneral,
	}, func(access *sites.AccessResult) (sites.TargetUploadEvidence, error) {
		return client.upload(ctx, request, actor, access)
	})
}

func (client *Client) upload(ctx context.Context, request sites.TargetUploadRequest, actor workflow.Actor, access *sites.AccessResult) (sites.TargetUploadEvidence, error) {
	fields, inspection, err := validateUploadRequest(request)
	if err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("target_upload_request_invalid", err.Error(), false, err)
	}
	body, contentType, err := buildUploadBody(fields, request.Torrent)
	if err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("target_upload_request_invalid", "could not build the bounded MTEAM upload request", false, err)
	}
	runtime, err := client.store.GetRuntimeSite(ctx, "MTEAM")
	if err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("site_configuration_unavailable", "MTEAM site configuration is unavailable or disabled", false, err)
	}
	if runtime.Adapter != "mteam_api" {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("site_adapter_mismatch", "configured MTEAM site adapter is not mteam_api", false, nil)
	}
	apiKey := strings.TrimSpace(runtime.Credentials["api_key"])
	if apiKey == "" {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("site_api_key_required", "an enabled MTEAM api_key credential is required", false, nil)
	}
	config, endpoint, err := parseAPIConfig(runtime.Config)
	if err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("site_configuration_invalid", err.Error(), false, err)
	}
	configurationSHA := runtime.ConfigurationSHA256
	if configurationSHA == "" {
		digest := sha256.Sum256(runtime.Config)
		configurationSHA = hex.EncodeToString(digest[:])
	}
	intent := uploadAuditDetails(request, configurationSHA)
	intent["v1_infohash"] = inspection.Hashes.V1SHA1
	if err := client.store.AuditSiteAction(ctx, "MTEAM", "target.upload_intent", intent, actor); err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("target_upload_audit_failed", "could not persist the MTEAM upload intent; no upload was attempted", false, err)
	}

	requestURL := resolveAPI(endpoint, "/api/torrent/createOredit")
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL.String(), bytes.NewReader(body))
	if err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("target_upload_request_invalid", "could not build the MTEAM upload request", false, err)
	}
	httpRequest.Header.Set("Accept", "application/json")
	httpRequest.Header.Set("Content-Type", contentType)
	httpRequest.Header.Set("User-Agent", "Upload-Assistant-Go/2")
	httpRequest.Header.Set("x-api-key", apiKey)
	httpClient := *client.httpClient
	if config.TimeoutSeconds > 0 {
		httpClient.Timeout = time.Duration(config.TimeoutSeconds) * time.Second
	}
	response, err := httpClient.Do(httpRequest)
	if err != nil {
		client.auditUploadOutcome(ctx, request, configurationSHA, "unknown", "network_error", "", actor)
		return sites.TargetUploadEvidence{}, sites.NewAdapterError(
			"target_upload_outcome_unknown", "MTEAM upload connection ended without a trustworthy result; run a fresh duplicate check before any retry", false, nil,
		)
	}
	defer response.Body.Close()
	access.StatusCode = response.StatusCode
	responseBody, readErr := io.ReadAll(io.LimitReader(response.Body, maxAPIResponse+1))
	if readErr != nil || len(responseBody) > maxAPIResponse {
		client.auditUploadOutcome(ctx, request, configurationSHA, "unknown", "unreadable_response", "", actor)
		return sites.TargetUploadEvidence{}, sites.NewAdapterError(
			"target_upload_outcome_unknown", "MTEAM upload response was unreadable or too large; run a fresh duplicate check before any retry", false, readErr,
		)
	}
	responseDigest := sha256.Sum256(responseBody)
	responseSHA := hex.EncodeToString(responseDigest[:])
	access.ResponseSHA256 = responseSHA
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		client.auditUploadOutcome(ctx, request, configurationSHA, "rejected", "authentication_failed", responseSHA, actor)
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("site_authentication_failed", "MTEAM rejected the API key before accepting the upload", false, nil)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode >= 500 {
			client.auditUploadOutcome(ctx, request, configurationSHA, "unknown", "server_error", responseSHA, actor)
			return sites.TargetUploadEvidence{}, sites.NewAdapterError(
				"target_upload_outcome_unknown", fmt.Sprintf("MTEAM returned HTTP %d; run a fresh duplicate check before any retry", response.StatusCode), false, nil,
			)
		}
		client.auditUploadOutcome(ctx, request, configurationSHA, "rejected", "http_error", responseSHA, actor)
		return sites.TargetUploadEvidence{}, sites.NewAdapterError("target_upload_rejected", fmt.Sprintf("MTEAM rejected the upload with HTTP %d", response.StatusCode), false, nil)
	}
	torrentID, err := parseUploadResponse(responseBody)
	if err != nil {
		client.auditUploadOutcome(ctx, request, configurationSHA, "unknown", "response_without_id", responseSHA, actor)
		return sites.TargetUploadEvidence{}, sites.NewAdapterError(
			"target_upload_outcome_unknown", "MTEAM returned success without a trustworthy torrent id; reconcile by duplicate check before any retry", false, err,
		)
	}
	evidence := sites.TargetUploadEvidence{
		SiteCode: "MTEAM", Adapter: runtime.Adapter, ConfigurationSHA256: configurationSHA,
		TorrentID: torrentID, DetailsURL: "https://kp.m-team.cc/details/" + torrentID,
		ResponseSHA256: responseSHA, SubmittedAt: time.Now().UTC(),
	}
	resultDetails := uploadAuditDetails(request, configurationSHA)
	resultDetails["torrent_id"] = torrentID
	resultDetails["response_sha256"] = responseSHA
	resultDetails["details_url"] = evidence.DetailsURL
	if err := client.store.AuditSiteAction(ctx, "MTEAM", "target.upload_result", resultDetails, actor); err != nil {
		return sites.TargetUploadEvidence{}, sites.NewAdapterError(
			"target_upload_outcome_unknown", "MTEAM accepted the upload but the result audit could not be persisted; reconcile before any retry", false, err,
		)
	}
	return evidence, nil
}

func validateUploadRequest(request sites.TargetUploadRequest) (map[string]string, torrentmeta.Inspection, error) {
	if !request.Confirmed {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("explicit confirm_upload=true is required")
	}
	if request.JobID == "" || request.AttemptID == "" || !validSHA256(request.PackageSHA256) || !validSHA256(request.TorrentSHA256) ||
		!validSHA256(request.ContentFingerprintSHA256) || !validSHA256(request.RuleFingerprint) || !validSHA256(request.DuplicateCheckSHA256) {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("upload audit bindings are incomplete")
	}
	if request.Package.Target != "MTEAM" || request.Package.Adapter != "mteam_api" || request.Package.SchemaVersion != 1 {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("target package is not a current MTEAM API package")
	}
	if len(request.Torrent) == 0 || len(request.Torrent) > maxUploadTorrentBytes {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("target torrent is empty or too large")
	}
	torrentDigest := sha256.Sum256(request.Torrent)
	if hex.EncodeToString(torrentDigest[:]) != request.TorrentSHA256 {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("target torrent SHA-256 does not match the upload binding")
	}
	inspection, err := torrentmeta.Inspect(request.Torrent)
	if err != nil || inspection.Announce != uploadAnnounceURL || inspection.Source != "MTEAM" || !inspection.Private ||
		!inspection.PrivateSet || !slices.Equal(inspection.TopLevelKeys, []string{"announce", "info"}) ||
		len(inspection.ExtraTopLevelKeys) != 0 || len(inspection.ExtraInfoKeys) != 0 {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("target torrent does not satisfy the MTEAM sanitized upload profile")
	}
	if inspection.ContentFingerprint != request.ContentFingerprintSHA256 || inspection.FileCount != request.Package.Content.FileCount ||
		inspection.TotalSizeBytes != request.Package.Content.TotalSizeBytes {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("target torrent payload does not match package content evidence")
	}
	mediaInfo, err := decodeMediaEvidence(request.Package.MediaInfo)
	if err != nil {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM package media evidence is invalid or too large")
	}
	description := strings.TrimSpace(request.Package.Description)
	if description == "" || len(description) > maxUploadDescriptionBytes {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM package description is empty or too large")
	}
	name, ok := boundedCleanString(request.Package.FormFields["name"], 255)
	if !ok {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM upload name is missing or invalid")
	}
	smallDescription, ok := boundedCleanString(request.Package.FormFields["smallDescr"], 255)
	if !ok {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM upload smallDescr is missing or invalid")
	}
	category, ok := boundedInteger(request.Package.FormFields["category"], 1, 100_000)
	if !ok {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM upload category is invalid")
	}
	standard, ok := boundedInteger(request.Package.FormFields["standard"], 1, 7)
	if !ok || !validStandard(int(standard)) {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM upload standard is invalid")
	}
	anonymous, ok := request.Package.FormFields["anonymous"].(bool)
	if !ok {
		return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM upload anonymous flag is invalid")
	}
	fields := map[string]string{
		"name": name, "smallDescr": smallDescription, "descr": request.Package.Description,
		"category": strconv.FormatInt(category, 10), "standard": strconv.FormatInt(standard, 10),
		"anonymous": strconv.FormatBool(anonymous), "dmmCode": "", "tags": "", "aids": "",
		"mediainfo": mediaInfo,
	}
	for _, key := range []string{"imdb", "douban"} {
		if value, exists := request.Package.FormFields[key]; exists {
			text, ok := boundedCleanString(value, 2_048)
			if !ok || !validMetadataURL(key, text) {
				return nil, torrentmeta.Inspection{}, fmt.Errorf("MTEAM upload %s URL is invalid", key)
			}
			fields[key] = text
		}
	}
	return fields, inspection, nil
}

func decodeMediaEvidence(raw json.RawMessage) (string, error) {
	body := bytes.TrimSpace(raw)
	if len(body) == 0 || len(body) > maxUploadMediaInfoBytes || !json.Valid(body) {
		return "", fmt.Errorf("media evidence envelope is invalid")
	}
	text := ""
	if body[0] == '"' {
		if err := json.Unmarshal(body, &text); err != nil {
			return "", fmt.Errorf("decode media evidence text: %w", err)
		}
	} else if body[0] == '{' {
		// Compatibility with packages persisted before media evidence was
		// normalized to a JSON string.
		text = string(body)
	} else {
		return "", fmt.Errorf("media evidence must be text or a legacy JSON object")
	}
	text = strings.TrimSpace(text)
	if text == "" || len(text) > maxUploadMediaInfoBytes || strings.IndexByte(text, 0) >= 0 || !utf8.ValidString(text) {
		return "", fmt.Errorf("media evidence text is invalid")
	}
	return text, nil
}

func buildUploadBody(fields map[string]string, torrent []byte) ([]byte, string, error) {
	buffer := bytes.NewBuffer(nil)
	writer := multipart.NewWriter(buffer)
	keys := make([]string, 0, len(fields))
	for key := range fields {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if err := writer.WriteField(key, fields[key]); err != nil {
			return nil, "", err
		}
	}
	header := make(textproto.MIMEHeader)
	header.Set("Content-Disposition", `form-data; name="file"; filename="mteam-upload.torrent"`)
	header.Set("Content-Type", "application/x-bittorrent")
	part, err := writer.CreatePart(header)
	if err != nil {
		return nil, "", err
	}
	if _, err := part.Write(torrent); err != nil {
		return nil, "", err
	}
	if err := writer.Close(); err != nil {
		return nil, "", err
	}
	if buffer.Len() > maxUploadRequestBytes {
		return nil, "", fmt.Errorf("multipart request exceeds %d bytes", maxUploadRequestBytes)
	}
	return buffer.Bytes(), writer.FormDataContentType(), nil
}

func parseUploadResponse(body []byte) (string, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var response struct {
		Code any             `json:"code"`
		Data json.RawMessage `json:"data"`
	}
	if err := decoder.Decode(&response); err != nil || !successCode(response.Code) || len(response.Data) == 0 {
		return "", fmt.Errorf("unsuccessful API envelope")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return "", fmt.Errorf("trailing API response value")
	}
	return extractUploadID(response.Data)
}

func extractUploadID(body json.RawMessage) (string, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return "", err
	}
	var candidate string
	switch typed := value.(type) {
	case string:
		candidate = strings.TrimSpace(typed)
	case json.Number:
		candidate = typed.String()
	case map[string]any:
		for _, key := range []string{"id", "torrentId", "torrent_id"} {
			if raw, exists := typed[key]; exists {
				switch id := raw.(type) {
				case string:
					candidate = strings.TrimSpace(id)
				case json.Number:
					candidate = id.String()
				}
				if candidate != "" {
					break
				}
			}
		}
		if candidate == "" {
			if nested, exists := typed["data"]; exists {
				nestedBody, _ := json.Marshal(nested)
				return extractUploadID(nestedBody)
			}
		}
	}
	if len(candidate) == 0 || len(candidate) > 20 {
		return "", fmt.Errorf("upload response has no torrent id")
	}
	for _, character := range candidate {
		if character < '0' || character > '9' {
			return "", fmt.Errorf("upload response torrent id is invalid")
		}
	}
	return candidate, nil
}

func uploadAuditDetails(request sites.TargetUploadRequest, configurationSHA string) map[string]any {
	return map[string]any{
		"job_id": request.JobID, "attempt_id": request.AttemptID,
		"package_sha256": request.PackageSHA256, "torrent_sha256": request.TorrentSHA256,
		"content_fingerprint_sha256": request.ContentFingerprintSHA256,
		"rule_fingerprint":           request.RuleFingerprint, "duplicate_check_sha256": request.DuplicateCheckSHA256,
		"configuration_sha256": configurationSHA,
	}
}

func (client *Client) auditUploadOutcome(ctx context.Context, request sites.TargetUploadRequest, configurationSHA, status, reason, responseSHA string, actor workflow.Actor) {
	details := uploadAuditDetails(request, configurationSHA)
	details["status"], details["reason"] = status, reason
	if responseSHA != "" {
		details["response_sha256"] = responseSHA
	}
	_ = client.store.AuditSiteAction(ctx, "MTEAM", "target.upload_outcome", details, actor)
}

func boundedCleanString(value any, maxRunes int) (string, bool) {
	text, ok := value.(string)
	text = strings.TrimSpace(text)
	if !ok || text == "" || utf8.RuneCountInString(text) > maxRunes {
		return "", false
	}
	for _, character := range text {
		if character == '\x00' || character == '\r' || character == '\n' || unicode.IsControl(character) {
			return "", false
		}
	}
	return text, true
}

func boundedInteger(value any, minimum, maximum int64) (int64, bool) {
	var number int64
	switch typed := value.(type) {
	case int:
		number = int64(typed)
	case int64:
		number = typed
	case float64:
		number = int64(typed)
		if float64(number) != typed {
			return 0, false
		}
	case json.Number:
		var err error
		number, err = typed.Int64()
		if err != nil {
			return 0, false
		}
	default:
		return 0, false
	}
	return number, number >= minimum && number <= maximum
}

func validMetadataURL(kind, value string) bool {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return false
	}
	switch kind {
	case "imdb":
		segments := strings.Split(strings.Trim(parsed.Path, "/"), "/")
		return parsed.Hostname() == "www.imdb.com" && len(segments) == 2 && segments[0] == "title" && imdbPattern.MatchString(segments[1])
	case "douban":
		return parsed.Hostname() == "movie.douban.com" && strings.HasPrefix(parsed.Path, "/subject/")
	default:
		return false
	}
}

func validSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
