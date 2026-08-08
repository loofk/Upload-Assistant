package imagehosts

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
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var (
	ErrAdapterUnavailable   = errors.New("image host adapter is not implemented")
	ErrUploadOutcomeUnknown = errors.New("image host upload outcome is unknown")
)

const (
	maxImageBytes    = 32 << 20
	maxResponseBytes = 4 << 20
)

type ConfigurationStore interface {
	GetRuntimeImageHost(context.Context, string) (integrations.RuntimeImageHost, error)
	RecordImageHostHealth(context.Context, string, string, map[string]any, workflow.Actor) error
	AuditImageHostAction(context.Context, string, string, map[string]any, workflow.Actor) error
}

type Image struct {
	Filename string
	MIMEType string
	Bytes    []byte
	SHA256   string
}

type UploadResult struct {
	URL          string `json:"url"`
	ViewerURL    string `json:"viewer_url,omitempty"`
	ThumbnailURL string `json:"thumbnail_url,omitempty"`
	RemoteID     string `json:"remote_id,omitempty"`
	Extension    string `json:"extension,omitempty"`
}

type UploadEvidence struct {
	ImageHostID       string       `json:"image_host_id"`
	ImageHostName     string       `json:"image_host_name"`
	Adapter           string       `json:"adapter"`
	ConfigSHA256      string       `json:"config_sha256"`
	ConfigurationTime time.Time    `json:"configuration_updated_at"`
	SourceFilename    string       `json:"source_filename"`
	SourceMIMEType    string       `json:"source_mime_type"`
	SourceSizeBytes   int64        `json:"source_size_bytes"`
	SourceSHA256      string       `json:"source_sha256"`
	Result            UploadResult `json:"result"`
}

type HostSnapshot struct {
	ID                string    `json:"id"`
	Name              string    `json:"name"`
	Adapter           string    `json:"adapter"`
	ConfigSHA256      string    `json:"config_sha256"`
	ConfigurationTime time.Time `json:"configuration_updated_at"`
}

type Manager struct {
	store      ConfigurationStore
	httpClient *http.Client
}

func NewManager(store ConfigurationStore, httpClient *http.Client) *Manager {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 90 * time.Second}
	} else {
		clone := *httpClient
		httpClient = &clone
		if httpClient.Timeout <= 0 {
			httpClient.Timeout = 90 * time.Second
		}
	}
	httpClient.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return errors.New("image host redirect is not allowed")
	}
	return &Manager{store: store, httpClient: httpClient}
}

func (manager *Manager) Snapshot(ctx context.Context, name string) (HostSnapshot, error) {
	runtime, err := manager.store.GetRuntimeImageHost(ctx, name)
	if err != nil {
		return HostSnapshot{}, err
	}
	if runtime.Adapter != "imgbb" && runtime.Adapter != "ptpimg" {
		return HostSnapshot{}, fmt.Errorf("%w: %s", ErrAdapterUnavailable, runtime.Adapter)
	}
	if _, err := validateEndpoint(runtime.EndpointConfig.Endpoint); err != nil {
		return HostSnapshot{}, err
	}
	digest := sha256.Sum256(runtime.Config)
	return HostSnapshot{
		ID: runtime.ID, Name: runtime.Name, Adapter: runtime.Adapter,
		ConfigSHA256: hex.EncodeToString(digest[:]), ConfigurationTime: runtime.UpdatedAt,
	}, nil
}

func (manager *Manager) Upload(ctx context.Context, name string, image Image, actor workflow.Actor) (UploadEvidence, error) {
	if err := validateImage(image); err != nil {
		return UploadEvidence{}, err
	}
	runtime, err := manager.store.GetRuntimeImageHost(ctx, name)
	if err != nil {
		return UploadEvidence{}, err
	}
	endpoint, err := validateEndpoint(runtime.EndpointConfig.Endpoint)
	if err != nil {
		return UploadEvidence{}, err
	}
	configHash := sha256.Sum256(runtime.Config)
	configurationSHA := hex.EncodeToString(configHash[:])
	if err := manager.store.AuditImageHostAction(ctx, name, "image.upload_intent", map[string]any{
		"adapter": runtime.Adapter, "source_filename": image.Filename,
		"source_size_bytes": len(image.Bytes), "source_sha256": strings.ToLower(image.SHA256),
		"config_sha256": configurationSHA,
	}, actor); err != nil {
		return UploadEvidence{}, fmt.Errorf("persist image upload intent: %w", err)
	}
	var result UploadResult
	switch runtime.Adapter {
	case "imgbb":
		result, err = manager.uploadImgBB(ctx, endpoint, runtime.Credentials, image)
	case "ptpimg":
		result, err = manager.uploadPTPImg(ctx, endpoint, runtime.Credentials, image)
	default:
		return UploadEvidence{}, fmt.Errorf("%w: %s", ErrAdapterUnavailable, runtime.Adapter)
	}
	if err != nil {
		manager.recordHealth(ctx, name, "failed", map[string]any{"adapter": runtime.Adapter, "error": safeMessage(err.Error())}, actor)
		return UploadEvidence{}, err
	}
	evidence := UploadEvidence{
		ImageHostID: runtime.ID, ImageHostName: runtime.Name, Adapter: runtime.Adapter,
		ConfigSHA256: configurationSHA, ConfigurationTime: runtime.UpdatedAt,
		SourceFilename: image.Filename, SourceMIMEType: image.MIMEType,
		SourceSizeBytes: int64(len(image.Bytes)), SourceSHA256: strings.ToLower(image.SHA256), Result: result,
	}
	if err := ValidateUploadEvidence(evidence, image); err != nil {
		return evidence, fmt.Errorf("%w: successful response evidence is invalid", ErrUploadOutcomeUnknown)
	}
	if err := manager.store.AuditImageHostAction(ctx, name, "image.upload", map[string]any{
		"adapter": runtime.Adapter, "source_filename": image.Filename,
		"source_size_bytes": len(image.Bytes), "source_sha256": evidence.SourceSHA256,
		"config_sha256": evidence.ConfigSHA256, "remote_id": result.RemoteID,
		"url_host": urlHost(result.URL),
	}, actor); err != nil {
		return evidence, fmt.Errorf("%w: persist image upload result audit", ErrUploadOutcomeUnknown)
	}
	manager.recordHealth(ctx, name, "ready", map[string]any{"adapter": runtime.Adapter}, actor)
	return evidence, nil
}

func (manager *Manager) uploadImgBB(ctx context.Context, endpoint *url.URL, credentials map[string]string, image Image) (UploadResult, error) {
	apiKey := strings.TrimSpace(credentials["api_key"])
	if apiKey == "" {
		return UploadResult{}, fmt.Errorf("imgbb api_key credential is required")
	}
	responseBody, err := manager.multipart(ctx, endpoint, []formField{{"key", apiKey}}, "image", image, "")
	if err != nil {
		return UploadResult{}, err
	}
	var response struct {
		Success bool `json:"success"`
		Status  int  `json:"status"`
		Data    struct {
			ID         string `json:"id"`
			URL        string `json:"url"`
			DisplayURL string `json:"display_url"`
			ViewerURL  string `json:"url_viewer"`
			Image      struct {
				URL string `json:"url"`
			} `json:"image"`
			Thumb struct {
				URL string `json:"url"`
			} `json:"thumb"`
		} `json:"data"`
	}
	if err := json.Unmarshal(responseBody, &response); err != nil || !response.Success || response.Status < 200 || response.Status >= 300 {
		return UploadResult{}, fmt.Errorf("%w: imgbb returned an unsuccessful or invalid success response", ErrUploadOutcomeUnknown)
	}
	rawURL := firstNonEmpty(response.Data.Image.URL, response.Data.URL, response.Data.DisplayURL)
	if err := validatePublicImageURL(rawURL, []string{"i.ibb.co", "ibb.co"}); err != nil {
		return UploadResult{}, fmt.Errorf("%w: imgbb response URL is invalid", ErrUploadOutcomeUnknown)
	}
	result := UploadResult{URL: rawURL, RemoteID: response.Data.ID}
	if validatePublicImageURL(response.Data.ViewerURL, []string{"ibb.co"}) == nil {
		result.ViewerURL = response.Data.ViewerURL
	}
	if validatePublicImageURL(response.Data.Thumb.URL, []string{"i.ibb.co", "ibb.co"}) == nil {
		result.ThumbnailURL = response.Data.Thumb.URL
	}
	return result, nil
}

func (manager *Manager) uploadPTPImg(ctx context.Context, endpoint *url.URL, credentials map[string]string, image Image) (UploadResult, error) {
	apiKey := strings.TrimSpace(credentials["api_key"])
	if apiKey == "" {
		return UploadResult{}, fmt.Errorf("ptpimg api_key credential is required")
	}
	responseBody, err := manager.multipart(ctx, endpoint, []formField{{"format", "json"}, {"api_key", apiKey}}, "file-upload[0]", image, endpoint.Scheme+"://"+endpoint.Host+"/index.php")
	if err != nil {
		return UploadResult{}, err
	}
	var response []struct {
		Code string `json:"code"`
		Ext  string `json:"ext"`
	}
	if err := json.Unmarshal(responseBody, &response); err != nil || len(response) == 0 || !ptpCodePattern.MatchString(response[0].Code) || !extensionPattern.MatchString(response[0].Ext) {
		return UploadResult{}, fmt.Errorf("%w: ptpimg returned an unsuccessful or invalid success response", ErrUploadOutcomeUnknown)
	}
	imageURL := "https://ptpimg.me/" + response[0].Code + "." + strings.ToLower(response[0].Ext)
	return UploadResult{URL: imageURL, ViewerURL: imageURL, RemoteID: response[0].Code, Extension: strings.ToLower(response[0].Ext)}, nil
}

type formField struct{ name, value string }

func (manager *Manager) multipart(ctx context.Context, endpoint *url.URL, fields []formField, fileField string, image Image, referer string) ([]byte, error) {
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for _, field := range fields {
		if err := writer.WriteField(field.name, field.value); err != nil {
			return nil, fmt.Errorf("build image host request")
		}
	}
	part, err := writer.CreateFormFile(fileField, image.Filename)
	if err != nil {
		return nil, fmt.Errorf("build image host file request")
	}
	if _, err := part.Write(image.Bytes); err != nil {
		return nil, fmt.Errorf("build image host file body")
	}
	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("finalize image host request")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint.String(), &body)
	if err != nil {
		return nil, fmt.Errorf("build image host request")
	}
	request.Header.Set("Content-Type", writer.FormDataContentType())
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", "Upload-Assistant/2")
	if referer != "" {
		request.Header.Set("Referer", referer)
	}
	response, err := manager.httpClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("%w: image host request ended without a response", ErrUploadOutcomeUnknown)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil || len(responseBody) > maxResponseBytes {
		return nil, fmt.Errorf("%w: image host response is unreadable or too large", ErrUploadOutcomeUnknown)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode >= 500 {
			return nil, fmt.Errorf("%w: image host returned HTTP %d", ErrUploadOutcomeUnknown, response.StatusCode)
		}
		return nil, fmt.Errorf("image host returned HTTP %d", response.StatusCode)
	}
	return responseBody, nil
}

func validateImage(image Image) error {
	if strings.TrimSpace(image.Filename) == "" || strings.ContainsAny(image.Filename, "/\\\r\n") {
		return fmt.Errorf("image filename is invalid")
	}
	if image.MIMEType != "image/png" && image.MIMEType != "image/jpeg" && image.MIMEType != "image/webp" {
		return fmt.Errorf("unsupported image MIME type")
	}
	if len(image.Bytes) == 0 || len(image.Bytes) > maxImageBytes {
		return fmt.Errorf("image must be between 1 byte and %d bytes", maxImageBytes)
	}
	if !validImageBytes(image.MIMEType, image.Bytes) {
		return fmt.Errorf("image bytes do not match the declared MIME type")
	}
	digest := sha256.Sum256(image.Bytes)
	if !strings.EqualFold(image.SHA256, hex.EncodeToString(digest[:])) {
		return fmt.Errorf("image SHA-256 does not match its bytes")
	}
	return nil
}

func ValidateUploadEvidence(evidence UploadEvidence, image Image) error {
	if err := validateImage(image); err != nil {
		return err
	}
	if evidence.ImageHostID == "" || evidence.ImageHostName == "" || len(evidence.ConfigSHA256) != sha256.Size*2 ||
		evidence.ConfigurationTime.IsZero() || evidence.SourceFilename != image.Filename ||
		evidence.SourceMIMEType != image.MIMEType || evidence.SourceSizeBytes != int64(len(image.Bytes)) ||
		!strings.EqualFold(evidence.SourceSHA256, image.SHA256) || evidence.Result.URL == "" {
		return fmt.Errorf("image upload evidence is incomplete or bound to another source")
	}
	switch evidence.Adapter {
	case "imgbb":
		if err := validatePublicImageURL(evidence.Result.URL, []string{"i.ibb.co", "ibb.co"}); err != nil {
			return err
		}
		if evidence.Result.ViewerURL != "" {
			if err := validatePublicImageURL(evidence.Result.ViewerURL, []string{"ibb.co"}); err != nil {
				return err
			}
		}
		if evidence.Result.ThumbnailURL != "" {
			if err := validatePublicImageURL(evidence.Result.ThumbnailURL, []string{"i.ibb.co", "ibb.co"}); err != nil {
				return err
			}
		}
	case "ptpimg":
		if err := validatePublicImageURL(evidence.Result.URL, []string{"ptpimg.me"}); err != nil {
			return err
		}
		if evidence.Result.ViewerURL != "" && evidence.Result.ViewerURL != evidence.Result.URL {
			return fmt.Errorf("ptpimg viewer URL does not match the image URL")
		}
	default:
		return fmt.Errorf("unsupported image upload evidence adapter")
	}
	return nil
}

func validImageBytes(mimeType string, body []byte) bool {
	switch mimeType {
	case "image/png":
		return len(body) >= 8 && string(body[:8]) == "\x89PNG\r\n\x1a\n"
	case "image/jpeg":
		return len(body) >= 3 && body[0] == 0xff && body[1] == 0xd8 && body[2] == 0xff
	case "image/webp":
		return len(body) >= 12 && string(body[:4]) == "RIFF" && string(body[8:12]) == "WEBP"
	default:
		return false
	}
}

func validateEndpoint(value string) (*url.URL, error) {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "https" && parsed.Scheme != "http") {
		return nil, fmt.Errorf("image host endpoint is invalid")
	}
	host := strings.ToLower(parsed.Hostname())
	loopback := host == "localhost" || host == "127.0.0.1" || host == "::1"
	if parsed.Scheme != "https" && !loopback {
		return nil, fmt.Errorf("image host endpoint must use HTTPS")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("image host endpoint must not contain credentials, query, or fragment")
	}
	return parsed, nil
}

func validatePublicImageURL(value string, allowedHosts []string) error {
	if strings.ContainsAny(value, "[]\r\n\t ") {
		return fmt.Errorf("URL contains unsafe characters")
	}
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Host == "" {
		return fmt.Errorf("URL must be absolute HTTPS")
	}
	host := strings.ToLower(parsed.Hostname())
	for _, allowed := range allowedHosts {
		if host == allowed || strings.HasSuffix(host, "."+allowed) {
			return nil
		}
	}
	return fmt.Errorf("unexpected response host")
}

var (
	ptpCodePattern   = regexp.MustCompile(`^[A-Za-z0-9]{3,64}$`)
	extensionPattern = regexp.MustCompile(`(?i)^(png|jpe?g|webp|gif)$`)
)

func (manager *Manager) recordHealth(ctx context.Context, name, status string, details map[string]any, actor workflow.Actor) {
	recordCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
	defer cancel()
	_ = manager.store.RecordImageHostHealth(recordCtx, name, status, details, actor)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func urlHost(value string) string {
	parsed, _ := url.Parse(value)
	return parsed.Hostname()
}

func safeMessage(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\n", " "))
	if len(value) > 300 {
		return value[:300]
	}
	return value
}
