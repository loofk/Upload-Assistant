package operations

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

const (
	ProviderAPIModeChatCompletions = "chat_completions"
	ProviderAPIModeResponses       = "responses"
	ProviderProbeStageCatalog      = "catalog"
	ProviderProbeStageInference    = "inference"
	providerPayloadPreviewBytes    = 32 * 1024
	providerBufferedErrorBytes     = 1 << 20
	providerBufferedResponseBytes  = 8 << 20
	providerNormalizedOutputBytes  = 8 << 20
	providerSSEEventBytes          = 1 << 20
	providerDefaultOutputTokens    = 4096
	ruleAnalysisOutputTokens       = 8192
)

type ProviderModelCapability struct {
	ID               string   `json:"id"`
	ReasoningEfforts []string `json:"reasoning_efforts"`
	ReasoningSource  string   `json:"reasoning_source"`
}

type ProviderCapabilities struct {
	CatalogSource string                    `json:"catalog_source"`
	Models        []ProviderModelCapability `json:"models"`
	UpdatedAt     *time.Time                `json:"updated_at,omitempty"`
}

type ProviderProbeEvidence struct {
	Stage             string    `json:"stage"`
	EndpointPath      string    `json:"endpoint_path"`
	StatusCode        int       `json:"status_code,omitempty"`
	ContentType       string    `json:"content_type,omitempty"`
	ResponseSHA256    string    `json:"response_sha256,omitempty"`
	ResponseShape     []string  `json:"response_shape"`
	RequestID         string    `json:"request_id,omitempty"`
	TraceID           string    `json:"trace_id,omitempty"`
	LatencyMS         int64     `json:"latency_ms"`
	Streaming         bool      `json:"streaming"`
	ResponseHeadersMS int64     `json:"response_headers_ms,omitempty"`
	StreamEventCount  int       `json:"stream_event_count,omitempty"`
	StreamCompleted   bool      `json:"stream_completed,omitempty"`
	ErrorCode         string    `json:"error_code,omitempty"`
	PerformedAt       time.Time `json:"performed_at"`
}

type ProviderProbeResult struct {
	Status                string                `json:"status"`
	Stage                 string                `json:"stage"`
	ModelAvailable        bool                  `json:"model_available"`
	ExternalCallPerformed bool                  `json:"external_call_performed"`
	Evidence              ProviderProbeEvidence `json:"evidence"`
}

type providerProbeError struct {
	code   string
	detail string
}

func (e providerProbeError) Error() string { return e.detail }

func providerAPIEndpoint(rawBaseURL, dataLevel, suffix string) (string, string, error) {
	parsed, err := ValidateProviderURL(rawBaseURL, dataLevel)
	if err != nil {
		return "", "", err
	}
	cleanPath := strings.TrimRight(parsed.EscapedPath(), "/")
	if cleanPath == "" {
		cleanPath = "/v1"
	}
	decodedPath, err := url.PathUnescape(cleanPath)
	if err != nil {
		return "", "", fmt.Errorf("%w: provider URL path is invalid", ErrInvalid)
	}
	parsed.Path = decodedPath
	parsed.RawPath = ""
	base := strings.TrimRight(parsed.String(), "/")
	endpointPath := strings.TrimRight(parsed.Path, "/") + "/" + strings.TrimLeft(suffix, "/")
	return base + "/" + strings.TrimLeft(suffix, "/"), endpointPath, nil
}

func normalizeProviderBaseURL(rawBaseURL, dataLevel string) (string, error) {
	endpoint, _, err := providerAPIEndpoint(rawBaseURL, dataLevel, "models")
	if err != nil {
		return "", err
	}
	return strings.TrimSuffix(endpoint, "/models"), nil
}

func parseProviderCatalog(body []byte) (ProviderCapabilities, []string, error) {
	var root map[string]json.RawMessage
	if err := json.Unmarshal(body, &root); err != nil {
		return ProviderCapabilities{}, responseShape(body), errors.New("provider models response is not JSON")
	}
	shape := rawObjectShape(root)
	var items []json.RawMessage
	if raw, ok := root["data"]; !ok || json.Unmarshal(raw, &items) != nil || len(items) == 0 {
		return ProviderCapabilities{}, shape, errors.New("provider models response does not contain a non-empty data array")
	}
	if len(items) > 1000 {
		return ProviderCapabilities{}, shape, errors.New("provider models response exceeds the 1000 model limit")
	}
	models := make([]ProviderModelCapability, 0, len(items))
	seen := map[string]bool{}
	for _, raw := range items {
		var item map[string]json.RawMessage
		if json.Unmarshal(raw, &item) != nil {
			return ProviderCapabilities{}, shape, errors.New("provider model entry is invalid")
		}
		var id string
		if json.Unmarshal(item["id"], &id) != nil {
			return ProviderCapabilities{}, shape, errors.New("provider model entry is missing id")
		}
		id = strings.TrimSpace(id)
		if id == "" || len(id) > 200 || seen[id] {
			continue
		}
		seen[id] = true
		efforts := reportedReasoningEfforts(item)
		source := "unreported"
		if len(efforts) > 0 {
			source = "provider_reported"
		}
		models = append(models, ProviderModelCapability{ID: id, ReasoningEfforts: efforts, ReasoningSource: source})
	}
	if len(models) == 0 {
		return ProviderCapabilities{}, shape, errors.New("provider models response contains no usable model ids")
	}
	sort.Slice(models, func(i, j int) bool { return models[i].ID < models[j].ID })
	return ProviderCapabilities{CatalogSource: "provider_models", Models: models}, shape, nil
}

func reportedReasoningEfforts(item map[string]json.RawMessage) []string {
	for _, key := range []string{"reasoning_efforts", "supported_reasoning_efforts"} {
		if values := decodeEffortList(item[key]); len(values) > 0 {
			return values
		}
	}
	var capabilities map[string]json.RawMessage
	if json.Unmarshal(item["capabilities"], &capabilities) == nil {
		for _, key := range []string{"reasoning_efforts", "supported_reasoning_efforts"} {
			if values := decodeEffortList(capabilities[key]); len(values) > 0 {
				return values
			}
		}
	}
	return []string{}
}

func decodeEffortList(raw json.RawMessage) []string {
	var values []string
	if len(raw) == 0 || json.Unmarshal(raw, &values) != nil {
		return nil
	}
	seen := map[string]bool{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.ToLower(strings.TrimSpace(value))
		if validReasoningEffort(value) && value != "default" && !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	return result
}

func validReasoningEffort(value string) bool {
	switch value {
	case "default", "none", "minimal", "low", "medium", "high", "xhigh", "max":
		return true
	default:
		return false
	}
}

func responseShape(body []byte) []string {
	var root map[string]json.RawMessage
	if json.Unmarshal(body, &root) != nil {
		return []string{"non_json"}
	}
	return rawObjectShape(root)
}

func rawObjectShape(root map[string]json.RawMessage) []string {
	shape := make([]string, 0, len(root))
	for key, raw := range root {
		kind := "value"
		trimmed := strings.TrimSpace(string(raw))
		if strings.HasPrefix(trimmed, "[") {
			kind = "array"
		} else if strings.HasPrefix(trimmed, "{") {
			kind = "object"
		} else if strings.HasPrefix(trimmed, `"`) {
			kind = "string"
		}
		shape = append(shape, key+":"+kind)
	}
	sort.Strings(shape)
	if len(shape) > 24 {
		shape = shape[:24]
	}
	return shape
}

func responseDigest(body []byte) string {
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}

type providerCompletionUsage struct {
	InputTokens  int
	OutputTokens int
}

// ProviderPayloadEvidence retains a recursively redacted, bounded preview and
// an immutable digest of the complete body. The preview is deliberately
// limited so a large provider response cannot flood the operational log.
type ProviderPayloadEvidence struct {
	BodySHA256          string `json:"body_sha256,omitempty"`
	CapturedSHA256      string `json:"captured_sha256,omitempty"`
	BodyBytes           int    `json:"body_bytes"`
	BodyComplete        bool   `json:"body_complete"`
	Preview             string `json:"preview,omitempty"`
	PreviewBytes        int    `json:"preview_bytes"`
	PreviewTruncated    bool   `json:"preview_truncated"`
	PreviewOmittedBytes int    `json:"preview_omitted_bytes"`
	PreviewKind         string `json:"preview_kind,omitempty"`
}

// ProviderCallEvidence is the bounded, recursively redacted evidence kept for
// every inference request. Authorization headers and provider credentials are
// never included.
type ProviderCallEvidence struct {
	Action                string                  `json:"action,omitempty"`
	Method                string                  `json:"method,omitempty"`
	ProviderID            string                  `json:"provider_id,omitempty"`
	Model                 string                  `json:"model,omitempty"`
	APIMode               string                  `json:"api_mode,omitempty"`
	ReasoningEffort       string                  `json:"reasoning_effort,omitempty"`
	EndpointPath          string                  `json:"endpoint_path,omitempty"`
	TimeoutSeconds        int                     `json:"timeout_seconds,omitempty"`
	StatusCode            int                     `json:"status_code,omitempty"`
	ContentType           string                  `json:"content_type,omitempty"`
	ResponseSHA256        string                  `json:"response_sha256,omitempty"`
	ResponseShape         []string                `json:"response_shape,omitempty"`
	Request               ProviderPayloadEvidence `json:"request"`
	Response              ProviderPayloadEvidence `json:"response"`
	RequestID             string                  `json:"request_id,omitempty"`
	TraceID               string                  `json:"trace_id,omitempty"`
	LatencyMS             int64                   `json:"latency_ms"`
	Streaming             bool                    `json:"streaming"`
	ResponseHeadersMS     int64                   `json:"response_headers_ms,omitempty"`
	FirstEventMS          int64                   `json:"first_event_ms,omitempty"`
	LastEventMS           int64                   `json:"last_event_ms,omitempty"`
	MaxEventGapMS         int64                   `json:"max_event_gap_ms,omitempty"`
	StreamEventCount      int                     `json:"stream_event_count,omitempty"`
	StreamCompleted       bool                    `json:"stream_completed,omitempty"`
	StreamError           *ProviderStreamError    `json:"stream_error,omitempty"`
	Attempt               int                     `json:"attempt,omitempty"`
	MaxAttempts           int                     `json:"max_attempts,omitempty"`
	ExternalCallPerformed bool                    `json:"external_call_performed"`
	PerformedAt           time.Time               `json:"performed_at"`
}

type ProviderStreamMetrics struct {
	ResponseHeadersMS int64 `json:"response_headers_ms"`
	FirstEventMS      int64 `json:"first_event_ms"`
	LastEventMS       int64 `json:"last_event_ms"`
	MaxEventGapMS     int64 `json:"max_event_gap_ms"`
	TotalLatencyMS    int64 `json:"total_latency_ms"`
	EventCount        int   `json:"event_count"`
	Completed         bool  `json:"completed"`
	AttemptCount      int   `json:"attempt_count"`
	RecoveredByRetry  bool  `json:"recovered_by_retry"`
}

// ProviderStreamError is the bounded, recursively redacted part of an SSE
// error event that is useful for operators. Raw stream events and hidden
// reasoning are never persisted.
type ProviderStreamError struct {
	EventType string `json:"event_type,omitempty"`
	Type      string `json:"type,omitempty"`
	Code      string `json:"code,omitempty"`
	Message   string `json:"message,omitempty"`
	Retryable bool   `json:"retryable"`
}

type providerStreamEventError struct{ Evidence ProviderStreamError }

func (e providerStreamEventError) Error() string {
	label := e.Evidence.Code
	if label == "" {
		label = e.Evidence.Type
	}
	if label == "" {
		label = e.Evidence.EventType
	}
	if label == "" {
		label = "unknown"
	}
	if e.Evidence.Message == "" {
		return fmt.Sprintf("provider stream error (%s)", label)
	}
	return fmt.Sprintf("provider stream error (%s): %s", label, e.Evidence.Message)
}

func providerPayloadEvidence(body []byte) ProviderPayloadEvidence {
	if body == nil {
		return ProviderPayloadEvidence{}
	}
	digest := responseDigest(body)
	evidence := ProviderPayloadEvidence{BodySHA256: digest, CapturedSHA256: digest, BodyBytes: len(body), BodyComplete: true}
	var value any
	if json.Unmarshal(body, &value) == nil {
		redacted, _ := json.Marshal(Redact(value))
		body = redacted
	} else {
		redacted, _ := Redact(strings.ToValidUTF8(string(body), "�")).(string)
		body = []byte(redacted)
	}
	if len(body) > providerPayloadPreviewBytes {
		evidence.PreviewTruncated = true
		evidence.PreviewOmittedBytes = len(body) - providerPayloadPreviewBytes
		body = body[:providerPayloadPreviewBytes]
	}
	evidence.Preview = strings.ToValidUTF8(string(body), "�")
	evidence.PreviewBytes = len([]byte(evidence.Preview))
	return evidence
}

// providerRequestPayloadEvidence deliberately records only request structure.
// Prompts can contain private tracker rules, filenames, paths, or diagnostic
// evidence that is permitted at the model boundary but forbidden in logs.
// The complete request remains correlation-safe through its size and SHA-256.
func providerRequestPayloadEvidence(body []byte) ProviderPayloadEvidence {
	if body == nil {
		return ProviderPayloadEvidence{}
	}
	digest := responseDigest(body)
	evidence := ProviderPayloadEvidence{
		BodySHA256: digest, CapturedSHA256: digest, BodyBytes: len(body), BodyComplete: true,
		PreviewKind: "request_metadata",
	}
	metadata := map[string]any{}
	var request map[string]any
	if json.Unmarshal(body, &request) == nil {
		for _, key := range []string{"model", "stream", "max_completion_tokens", "max_output_tokens", "reasoning_effort"} {
			if value, ok := request[key]; ok {
				metadata[key] = value
			}
		}
		if reasoning, ok := request["reasoning"].(map[string]any); ok {
			metadata["reasoning"] = map[string]any{"effort": reasoning["effort"]}
		}
		if format, ok := request["response_format"].(map[string]any); ok {
			metadata["response_format"] = map[string]any{"type": format["type"]}
		}
		if text, ok := request["text"].(map[string]any); ok {
			if format, ok := text["format"].(map[string]any); ok {
				metadata["text_format"] = map[string]any{"type": format["type"]}
			}
		}
		for _, key := range []string{"messages", "input"} {
			items, ok := request[key].([]any)
			if !ok {
				continue
			}
			summary := make([]map[string]any, 0, len(items))
			for _, raw := range items {
				item, _ := raw.(map[string]any)
				content, _ := json.Marshal(item["content"])
				summary = append(summary, map[string]any{
					"role": item["role"], "content_bytes": len(content),
				})
			}
			metadata[key] = summary
		}
		fields := make([]string, 0, len(request))
		for key := range request {
			fields = append(fields, key)
		}
		sort.Strings(fields)
		metadata["fields"] = fields
	} else {
		metadata["shape"] = "non_json"
	}
	preview, _ := json.Marshal(metadata)
	if len(preview) > providerPayloadPreviewBytes {
		evidence.PreviewTruncated = true
		evidence.PreviewOmittedBytes = len(preview) - providerPayloadPreviewBytes
		preview = preview[:providerPayloadPreviewBytes]
	}
	evidence.Preview = strings.ToValidUTF8(string(preview), "�")
	evidence.PreviewBytes = len([]byte(evidence.Preview))
	return evidence
}

func incompleteProviderPayloadEvidence(body []byte) ProviderPayloadEvidence {
	evidence := providerPayloadEvidence(body)
	evidence.BodySHA256 = ""
	evidence.BodyComplete = false
	return evidence
}

func providerResponseMetadataEvidence(body []byte, output string, usage providerCompletionUsage, complete bool) ProviderPayloadEvidence {
	evidence := ProviderPayloadEvidence{
		BodyBytes: len(body), BodyComplete: complete, CapturedSHA256: responseDigest(body),
		PreviewKind: "response_metadata",
	}
	if complete {
		evidence.BodySHA256 = evidence.CapturedSHA256
	}
	metadata := map[string]any{
		"shape":        responseShape(body),
		"output_bytes": len([]byte(output)),
		"usage":        map[string]int{"input_tokens": usage.InputTokens, "output_tokens": usage.OutputTokens},
	}
	if output != "" {
		metadata["output_sha256"] = responseDigest([]byte(output))
	}
	preview, _ := json.Marshal(metadata)
	evidence.Preview = string(preview)
	evidence.PreviewBytes = len(preview)
	return evidence
}

func providerStreamingPayloadEvidence(raw []byte, output string, usage providerCompletionUsage, complete bool) ProviderPayloadEvidence {
	return providerStreamingPayloadEvidenceFromDigest(responseDigest(raw), len(raw), output, usage, complete)
}

func providerStreamingPayloadEvidenceFromDigest(digest string, bodyBytes int, output string, usage providerCompletionUsage, complete bool) ProviderPayloadEvidence {
	evidence := ProviderPayloadEvidence{
		CapturedSHA256: digest, BodyBytes: bodyBytes, BodyComplete: complete,
		PreviewKind: "normalized_stream_output",
	}
	if complete {
		evidence.BodySHA256 = digest
	}
	preview, _ := json.Marshal(map[string]any{
		"output_bytes": len([]byte(output)), "output_sha256": responseDigest([]byte(output)),
		"usage": map[string]int{"input_tokens": usage.InputTokens, "output_tokens": usage.OutputTokens},
	})
	if len(preview) > providerPayloadPreviewBytes {
		evidence.PreviewTruncated = true
		evidence.PreviewOmittedBytes = len(preview) - providerPayloadPreviewBytes
		preview = preview[:providerPayloadPreviewBytes]
	}
	evidence.Preview = strings.ToValidUTF8(string(preview), "�")
	evidence.PreviewBytes = len([]byte(evidence.Preview))
	return evidence
}

type providerStreamCapture struct {
	digest hash.Hash
	bytes  int
}

func newProviderStreamCapture() *providerStreamCapture {
	return &providerStreamCapture{digest: sha256.New()}
}

func (capture *providerStreamCapture) Write(value []byte) (int, error) {
	written, err := capture.digest.Write(value)
	capture.bytes += written
	return written, err
}

func (capture *providerStreamCapture) SHA256() string {
	return hex.EncodeToString(capture.digest.Sum(nil))
}

type ProviderCallFailure struct {
	Code     string               `json:"code"`
	Detail   string               `json:"detail"`
	Evidence ProviderCallEvidence `json:"evidence"`
}

func buildProviderCompletionRequest(provider Provider, system, user string, jsonMode bool, maxOutputTokens int) (string, string, []byte, error) {
	if maxOutputTokens < 1 {
		maxOutputTokens = 4096
	}
	suffix := "chat/completions"
	requestBody := map[string]any{
		"model":    provider.Model,
		"messages": []map[string]string{{"role": "system", "content": system}, {"role": "user", "content": user}},
	}
	if provider.APIMode == ProviderAPIModeResponses {
		suffix = "responses"
		requestBody = map[string]any{
			"model":             provider.Model,
			"input":             []map[string]string{{"role": "developer", "content": system}, {"role": "user", "content": user}},
			"max_output_tokens": maxOutputTokens,
		}
	} else {
		requestBody["max_completion_tokens"] = maxOutputTokens
	}
	applyReasoningEffort(requestBody, provider)
	if jsonMode {
		if provider.APIMode == ProviderAPIModeResponses {
			requestBody["text"] = map[string]any{"format": map[string]string{"type": "json_object"}}
		} else {
			requestBody["response_format"] = map[string]string{"type": "json_object"}
		}
	}
	body, err := json.Marshal(requestBody)
	if err != nil {
		return "", "", nil, err
	}
	endpoint, endpointPath, err := providerAPIEndpoint(provider.BaseURL, provider.DataLevel, suffix)
	return endpoint, endpointPath, body, err
}

func buildProviderStreamingCompletionRequest(provider Provider, system, user string, jsonMode bool, maxOutputTokens int) (string, string, []byte, error) {
	endpoint, endpointPath, body, err := buildProviderCompletionRequest(provider, system, user, jsonMode, maxOutputTokens)
	if err != nil {
		return "", "", nil, err
	}
	var requestBody map[string]any
	if err = json.Unmarshal(body, &requestBody); err != nil {
		return "", "", nil, err
	}
	requestBody["stream"] = true
	if provider.APIMode != ProviderAPIModeResponses {
		requestBody["stream_options"] = map[string]bool{"include_usage": true}
	}
	body, err = json.Marshal(requestBody)
	return endpoint, endpointPath, body, err
}

type providerCallError struct {
	code           string
	cause          error
	timeoutSeconds int
	evidence       ProviderCallEvidence
}

func (e providerCallError) Error() string { return e.cause.Error() }
func (e providerCallError) Unwrap() error { return e.cause }

func providerCallErrorCode(err error) string {
	var callErr providerCallError
	if errors.As(err, &callErr) {
		return callErr.code
	}
	return "provider_request_failed"
}

// DescribeProviderCallFailure converts a wrapped provider transport or schema
// failure into a stable user-safe code, explanation and body-free evidence.
func DescribeProviderCallFailure(err error) (ProviderCallFailure, bool) {
	var callErr providerCallError
	if !errors.As(err, &callErr) {
		return ProviderCallFailure{}, false
	}
	detail := "provider request could not be completed"
	switch callErr.code {
	case "provider_timeout":
		detail = fmt.Sprintf("provider request timed out after %d seconds", callErr.timeoutSeconds)
	case "provider_request_cancelled":
		detail = "provider request was cancelled before completion"
	case "provider_forbidden":
		detail = "provider address or data boundary is not permitted"
	case "request_invalid":
		detail = "provider request could not be built from the saved configuration"
	case "provider_secret_unavailable":
		detail = "provider credential could not be loaded"
	case "provider_response_failed":
		detail = "provider response could not be read"
	case "provider_response_too_large":
		if errors.Is(callErr.cause, errProviderNormalizedOutputTooLarge) {
			detail = "provider normalized output exceeded the 8 MiB safety limit"
		} else {
			detail = "provider buffered response exceeded the 8 MiB safety limit"
		}
	case "provider_stream_unsupported":
		detail = "provider did not return a server-sent event stream"
	case "provider_stream_error":
		detail = boundedLogText(callErr.cause.Error(), 4000)
	case "provider_stream_invalid":
		detail = "provider returned an invalid server-sent event stream"
	case "provider_stream_incomplete":
		detail = "provider stream ended before a completion event"
	case "provider_stream_event_too_large":
		detail = "provider returned an SSE event larger than the 1 MiB safety limit"
	case "provider_output_truncated":
		detail = "provider stopped because the output token limit was reached"
	case "provider_output_incomplete":
		detail = "provider stopped without a normal completion status"
	case "provider_configuration_changed":
		detail = "provider configuration changed after this work was queued; start a new request"
	case "provider_busy":
		detail = "provider inference capacity is full; retry after current analyses finish"
	case "provider_http_error":
		detail = fmt.Sprintf("provider returned HTTP %d", callErr.evidence.StatusCode)
	case "provider_schema_invalid":
		detail = "provider response did not match the configured API protocol"
	case "provider_output_invalid":
		detail = "provider returned invalid rule analysis JSON"
	case "rule_draft_invalid":
		detail = "provider rule draft failed authoritative validation"
	}
	return ProviderCallFailure{Code: callErr.code, Detail: detail, Evidence: callErr.evidence}, true
}

func providerOutputError(ctx context.Context, provider Provider, latency int64, code string, cause error) error {
	return providerCallStateError(ctx, provider, latency, code, cause, true)
}

func providerPreflightError(ctx context.Context, provider Provider, code string, cause error) error {
	return providerCallStateError(ctx, provider, 0, code, cause, false)
}

func providerCallStateError(ctx context.Context, provider Provider, latency int64, code string, cause error, externalCallPerformed bool) error {
	correlation := CorrelationFromContext(ctx)
	_, endpointPath, _ := providerAPIEndpoint(provider.BaseURL, provider.DataLevel, func() string {
		if provider.APIMode == ProviderAPIModeResponses {
			return "responses"
		}
		return "chat/completions"
	}())
	return providerCallError{code: code, cause: cause, timeoutSeconds: provider.TimeoutSeconds, evidence: ProviderCallEvidence{
		ProviderID: provider.ID, Model: provider.Model, APIMode: provider.APIMode,
		ReasoningEffort: provider.ReasoningEffort, EndpointPath: endpointPath,
		TimeoutSeconds: provider.TimeoutSeconds, RequestID: correlation.RequestID, TraceID: correlation.TraceID,
		LatencyMS: latency, ExternalCallPerformed: externalCallPerformed, PerformedAt: time.Now().UTC(),
	}}
}

func (s *DiagnosticService) authorizeProviderRequest(ctx context.Context, provider Provider, request *http.Request) error {
	if provider.secretID == "" {
		return nil
	}
	if s.Secrets == nil {
		return errors.New("provider secret manager is unavailable")
	}
	key, err := s.Secrets.Get(ctx, provider.secretID, "llm.provider."+provider.ID+".api_key")
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+string(key))
	return nil
}

func (s *DiagnosticService) doProviderCompletion(ctx context.Context, provider Provider, action, system, user string, jsonMode bool) (content string, usage providerCompletionUsage, latency int64, returnedErr error) {
	return s.doProviderCompletionWithLimit(ctx, provider, action, system, user, jsonMode, providerDefaultOutputTokens)
}

func (s *DiagnosticService) doProviderCompletionWithLimit(ctx context.Context, provider Provider, action, system, user string, jsonMode bool, maxOutputTokens int) (content string, usage providerCompletionUsage, latency int64, returnedErr error) {
	correlation := CorrelationFromContext(ctx)
	evidence := ProviderCallEvidence{
		Action:     action,
		Method:     http.MethodPost,
		ProviderID: provider.ID, Model: provider.Model, APIMode: provider.APIMode,
		ReasoningEffort: provider.ReasoningEffort, TimeoutSeconds: provider.TimeoutSeconds,
		RequestID: correlation.RequestID, TraceID: correlation.TraceID, PerformedAt: time.Now().UTC(),
	}
	fail := func(code string, cause error) error {
		return providerCallError{code: code, cause: cause, timeoutSeconds: provider.TimeoutSeconds, evidence: evidence}
	}
	defer func() {
		errorCode := ""
		if returnedErr != nil {
			errorCode = providerCallErrorCode(returnedErr)
		}
		s.recordProviderCall(evidence, errorCode, returnedErr)
	}()
	client, err := s.clientFor(provider)
	if err != nil {
		return "", providerCompletionUsage{}, 0, fail("provider_forbidden", err)
	}
	endpoint, endpointPath, body, err := buildProviderCompletionRequest(provider, system, user, jsonMode, maxOutputTokens)
	if err != nil {
		return "", providerCompletionUsage{}, 0, fail("request_invalid", err)
	}
	evidence.EndpointPath = endpointPath
	evidence.Request = providerRequestPayloadEvidence(body)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return "", providerCompletionUsage{}, 0, fail("request_invalid", err)
	}
	request.Header.Set("Content-Type", "application/json")
	if err = s.authorizeProviderRequest(ctx, provider, request); err != nil {
		return "", providerCompletionUsage{}, 0, fail("provider_secret_unavailable", err)
	}
	started := time.Now()
	evidence.PerformedAt = started.UTC()
	evidence.ExternalCallPerformed = true
	response, err := client.Do(request)
	evidence.ResponseHeadersMS = time.Since(started).Milliseconds()
	if err != nil {
		latency = time.Since(started).Milliseconds()
		evidence.LatencyMS = latency
		code := "provider_request_failed"
		if errors.Is(err, context.DeadlineExceeded) {
			code = "provider_timeout"
		} else if errors.Is(err, context.Canceled) {
			code = "provider_request_cancelled"
		} else if timeout, ok := err.(interface{ Timeout() bool }); ok && timeout.Timeout() {
			code = "provider_timeout"
		}
		return "", providerCompletionUsage{}, latency, fail(code, err)
	}
	defer response.Body.Close()
	evidence.StatusCode = response.StatusCode
	evidence.ContentType = strings.SplitN(response.Header.Get("Content-Type"), ";", 2)[0]
	responseLimit := providerBufferedResponseBytes
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		responseLimit = providerBufferedErrorBytes
	}
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, int64(responseLimit)+1))
	latency = time.Since(started).Milliseconds()
	evidence.LatencyMS = latency
	if err != nil {
		evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, false)
		return "", providerCompletionUsage{}, latency, fail("provider_response_failed", err)
	}
	if len(responseBody) > responseLimit {
		evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, false)
		return "", providerCompletionUsage{}, latency, fail("provider_response_too_large", fmt.Errorf("provider response exceeds %d bytes", responseLimit))
	}
	evidence.ResponseSHA256 = responseDigest(responseBody)
	evidence.ResponseShape = responseShape(responseBody)
	evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, true)
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return "", providerCompletionUsage{}, latency, fail("provider_http_error", fmt.Errorf("provider returned HTTP %d", response.StatusCode))
	}
	content, usage, err = parseProviderCompletion(provider, responseBody)
	evidence.Response = providerResponseMetadataEvidence(responseBody, content, usage, true)
	if err != nil {
		if errors.Is(err, errProviderOutputTruncated) {
			return "", providerCompletionUsage{}, latency, fail("provider_output_truncated", err)
		}
		if errors.Is(err, errProviderOutputIncomplete) {
			return "", providerCompletionUsage{}, latency, fail("provider_output_incomplete", err)
		}
		return "", providerCompletionUsage{}, latency, fail("provider_schema_invalid", err)
	}
	return content, usage, latency, nil
}

func (s *DiagnosticService) doProviderStreamingCompletion(ctx context.Context, provider Provider, action, system, user string, jsonMode bool) (content string, usage providerCompletionUsage, metrics ProviderStreamMetrics, returnedErr error) {
	return s.doProviderStreamingCompletionAttempt(ctx, provider, action, system, user, jsonMode, providerDefaultOutputTokens, 1, 1)
}

func (s *DiagnosticService) doProviderStreamingCompletionAttempt(ctx context.Context, provider Provider, action, system, user string, jsonMode bool, maxOutputTokens, attempt, maxAttempts int) (content string, usage providerCompletionUsage, metrics ProviderStreamMetrics, returnedErr error) {
	correlation := CorrelationFromContext(ctx)
	evidence := ProviderCallEvidence{
		Action: action, Method: http.MethodPost, ProviderID: provider.ID, Model: provider.Model,
		APIMode: provider.APIMode, ReasoningEffort: provider.ReasoningEffort,
		TimeoutSeconds: provider.TimeoutSeconds, RequestID: correlation.RequestID, TraceID: correlation.TraceID,
		Streaming: true, Attempt: attempt, MaxAttempts: maxAttempts, PerformedAt: time.Now().UTC(),
	}
	metrics.AttemptCount = attempt
	fail := func(code string, cause error) error {
		return providerCallError{code: code, cause: cause, timeoutSeconds: provider.TimeoutSeconds, evidence: evidence}
	}
	defer func() {
		errorCode := ""
		if returnedErr != nil {
			errorCode = providerCallErrorCode(returnedErr)
		}
		s.recordProviderCall(evidence, errorCode, returnedErr)
	}()
	client, err := s.clientFor(provider)
	if err != nil {
		return "", providerCompletionUsage{}, metrics, fail("provider_forbidden", err)
	}
	endpoint, endpointPath, body, err := buildProviderStreamingCompletionRequest(provider, system, user, jsonMode, maxOutputTokens)
	if err != nil {
		return "", providerCompletionUsage{}, metrics, fail("request_invalid", err)
	}
	evidence.EndpointPath = endpointPath
	evidence.Request = providerRequestPayloadEvidence(body)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return "", providerCompletionUsage{}, metrics, fail("request_invalid", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "text/event-stream")
	if err = s.authorizeProviderRequest(ctx, provider, request); err != nil {
		return "", providerCompletionUsage{}, metrics, fail("provider_secret_unavailable", err)
	}
	started := time.Now()
	evidence.PerformedAt = started.UTC()
	evidence.ExternalCallPerformed = true
	response, err := client.Do(request)
	metrics.ResponseHeadersMS = time.Since(started).Milliseconds()
	evidence.ResponseHeadersMS = metrics.ResponseHeadersMS
	if err != nil {
		code := providerTransportErrorCode(err)
		metrics.TotalLatencyMS = metrics.ResponseHeadersMS
		evidence.LatencyMS = metrics.TotalLatencyMS
		return "", providerCompletionUsage{}, metrics, fail(code, err)
	}
	defer response.Body.Close()
	evidence.StatusCode = response.StatusCode
	evidence.ContentType = strings.SplitN(response.Header.Get("Content-Type"), ";", 2)[0]
	responseLimit := providerBufferedResponseBytes
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		responseLimit = providerBufferedErrorBytes
	}
	limited := io.LimitReader(response.Body, int64(responseLimit)+1)
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		responseBody, readErr := io.ReadAll(limited)
		metrics.TotalLatencyMS = time.Since(started).Milliseconds()
		evidence.LatencyMS = metrics.TotalLatencyMS
		if len(responseBody) > responseLimit {
			evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, false)
			return "", providerCompletionUsage{}, metrics, fail("provider_response_too_large", fmt.Errorf("provider response exceeds %d bytes", responseLimit))
		}
		if readErr != nil {
			evidence.ResponseShape = responseShape(responseBody)
			evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, false)
			return "", providerCompletionUsage{}, metrics, fail(providerTransportErrorCode(readErr), readErr)
		}
		evidence.ResponseSHA256 = responseDigest(responseBody)
		evidence.ResponseShape = responseShape(responseBody)
		evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, true)
		return "", providerCompletionUsage{}, metrics, fail("provider_http_error", fmt.Errorf("provider returned HTTP %d", response.StatusCode))
	}
	if !strings.EqualFold(evidence.ContentType, "text/event-stream") {
		responseBody, readErr := io.ReadAll(limited)
		metrics.TotalLatencyMS = time.Since(started).Milliseconds()
		evidence.LatencyMS = metrics.TotalLatencyMS
		if len(responseBody) > responseLimit {
			evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, false)
			return "", providerCompletionUsage{}, metrics, fail("provider_response_too_large", fmt.Errorf("provider response exceeds %d bytes", responseLimit))
		}
		if readErr != nil {
			evidence.ResponseShape = responseShape(responseBody)
			evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, false)
			return "", providerCompletionUsage{}, metrics, fail(providerTransportErrorCode(readErr), readErr)
		}
		evidence.ResponseSHA256 = responseDigest(responseBody)
		evidence.ResponseShape = responseShape(responseBody)
		evidence.Response = providerResponseMetadataEvidence(responseBody, "", providerCompletionUsage{}, true)
		return "", providerCompletionUsage{}, metrics, fail("provider_stream_unsupported", errors.New("provider response is not text/event-stream"))
	}
	var lastEventAt time.Time
	onEvent := func() {
		now := time.Now()
		elapsed := now.Sub(started).Milliseconds()
		if lastEventAt.IsZero() {
			metrics.FirstEventMS = elapsed
		} else if gap := now.Sub(lastEventAt).Milliseconds(); gap > metrics.MaxEventGapMS {
			metrics.MaxEventGapMS = gap
		}
		metrics.LastEventMS = elapsed
		lastEventAt = now
	}
	capture := newProviderStreamCapture()
	content, usage, metrics.EventCount, metrics.Completed, err = parseProviderCompletionStream(provider, io.TeeReader(response.Body, capture), onEvent)
	metrics.TotalLatencyMS = time.Since(started).Milliseconds()
	evidence.LatencyMS = metrics.TotalLatencyMS
	evidence.FirstEventMS = metrics.FirstEventMS
	evidence.LastEventMS = metrics.LastEventMS
	evidence.MaxEventGapMS = metrics.MaxEventGapMS
	evidence.StreamEventCount = metrics.EventCount
	evidence.StreamCompleted = metrics.Completed
	streamDigest := capture.SHA256()
	evidence.ResponseShape = []string{"server_sent_events"}
	if err != nil {
		evidence.Response = providerStreamingPayloadEvidenceFromDigest(streamDigest, capture.bytes, content, usage, false)
		if errors.Is(err, errProviderNormalizedOutputTooLarge) {
			return "", providerCompletionUsage{}, metrics, fail("provider_response_too_large", err)
		}
		if errors.Is(err, errProviderSSEEventTooLarge) {
			return "", providerCompletionUsage{}, metrics, fail("provider_stream_event_too_large", err)
		}
		if errors.Is(err, errProviderOutputTruncated) {
			return "", providerCompletionUsage{}, metrics, fail("provider_output_truncated", err)
		}
		if errors.Is(err, errProviderOutputIncomplete) {
			return "", providerCompletionUsage{}, metrics, fail("provider_output_incomplete", err)
		}
		var streamErr providerStreamEventError
		if errors.As(err, &streamErr) {
			evidence.StreamError = &streamErr.Evidence
		}
		code := providerTransportErrorCode(err)
		if evidence.StreamError != nil {
			code = "provider_stream_error"
		} else if code == "provider_response_failed" {
			code = "provider_stream_invalid"
		}
		return "", providerCompletionUsage{}, metrics, fail(code, err)
	}
	if !metrics.Completed {
		evidence.Response = providerStreamingPayloadEvidenceFromDigest(streamDigest, capture.bytes, content, usage, false)
		return "", providerCompletionUsage{}, metrics, fail("provider_stream_incomplete", errors.New("provider stream ended without completion"))
	}
	if strings.TrimSpace(content) == "" {
		evidence.Response = providerStreamingPayloadEvidenceFromDigest(streamDigest, capture.bytes, content, usage, false)
		return "", providerCompletionUsage{}, metrics, fail("provider_stream_invalid", errors.New("provider stream contained no output text"))
	}
	evidence.ResponseSHA256 = streamDigest
	evidence.Response = providerStreamingPayloadEvidenceFromDigest(streamDigest, capture.bytes, content, usage, true)
	return content, usage, metrics, nil
}

// doConfiguredProviderCompletion is the single runtime dispatch point for
// inference. Streaming providers consume SSE incrementally so upstream proxies
// observe progress during long reasoning; providers can explicitly opt out.

type providerCompletionOptions struct {
	MaxOutputTokens       int
	RetryTransientFailure bool
}

func (s *DiagnosticService) doConfiguredProviderCompletion(ctx context.Context, provider Provider, action, system, user string, jsonMode bool) (content string, usage providerCompletionUsage, latency int64, metrics *ProviderStreamMetrics, err error) {
	return s.doConfiguredProviderCompletionWithOptions(ctx, provider, action, system, user, jsonMode, providerCompletionOptions{
		MaxOutputTokens: providerDefaultOutputTokens, RetryTransientFailure: true,
	})
}

func (s *DiagnosticService) doConfiguredProviderCompletionWithOptions(ctx context.Context, provider Provider, action, system, user string, jsonMode bool, options providerCompletionOptions) (content string, usage providerCompletionUsage, latency int64, metrics *ProviderStreamMetrics, err error) {
	release, acquireErr := s.acquireInference(ctx, provider.ID)
	if acquireErr != nil {
		code := providerTransportErrorCode(acquireErr)
		if errors.Is(acquireErr, errProviderInferenceBusy) {
			code = "provider_busy"
		}
		return "", providerCompletionUsage{}, 0, nil, providerPreflightError(ctx, provider, code, acquireErr)
	}
	defer release()
	if options.MaxOutputTokens < 1 {
		options.MaxOutputTokens = providerDefaultOutputTokens
	}
	if provider.StreamingEnabled {
		maxAttempts := 1
		if options.RetryTransientFailure {
			maxAttempts = 2
		}
		timeoutSeconds := provider.TimeoutSeconds
		if timeoutSeconds < 1 {
			timeoutSeconds = 60
		}
		budgetCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSeconds)*time.Second)
		defer cancel()
		streamContent, streamUsage, streamMetrics, streamErr := s.doProviderStreamingCompletionAttempt(budgetCtx, provider, action, system, user, jsonMode, options.MaxOutputTokens, 1, maxAttempts)
		if streamErr == nil || maxAttempts == 1 || !retryableConfiguredProviderFailure(streamErr) || budgetCtx.Err() != nil {
			return streamContent, streamUsage, streamMetrics.TotalLatencyMS, &streamMetrics, streamErr
		}
		firstLatency := streamMetrics.TotalLatencyMS
		streamContent, streamUsage, streamMetrics, streamErr = s.doProviderStreamingCompletionAttempt(budgetCtx, provider, action, system, user, jsonMode, options.MaxOutputTokens, 2, maxAttempts)
		streamMetrics.TotalLatencyMS += firstLatency
		streamMetrics.AttemptCount = 2
		streamMetrics.RecoveredByRetry = streamErr == nil
		return streamContent, streamUsage, streamMetrics.TotalLatencyMS, &streamMetrics, streamErr
	}
	content, usage, latency, err = s.doProviderCompletionWithLimit(ctx, provider, action, system, user, jsonMode, options.MaxOutputTokens)
	return content, usage, latency, nil, err
}

func retryableConfiguredProviderFailure(err error) bool {
	var streamErr providerStreamEventError
	if errors.As(err, &streamErr) {
		return streamErr.Evidence.Retryable
	}
	var callErr providerCallError
	if !errors.As(err, &callErr) || callErr.code != "provider_http_error" {
		return false
	}
	switch callErr.evidence.StatusCode {
	case http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout, 520, 521, 522, 523, 524:
		return true
	default:
		return false
	}
}

func providerTransportErrorCode(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return "provider_timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "provider_request_cancelled"
	}
	if timeout, ok := err.(interface{ Timeout() bool }); ok && timeout.Timeout() {
		return "provider_timeout"
	}
	return "provider_response_failed"
}

func providerStreamErrorPresent(raw json.RawMessage) bool {
	value := strings.TrimSpace(string(raw))
	return value != "" && value != "null" && value != "false" && value != `""` && value != "{}"
}

func decodeProviderStreamError(eventType string, raw json.RawMessage, fallback string) providerStreamEventError {
	evidence := ProviderStreamError{EventType: boundedLogText(eventType, 160)}
	var decoded any
	if providerStreamErrorPresent(raw) && json.Unmarshal(raw, &decoded) == nil {
		switch value := decoded.(type) {
		case string:
			evidence.Message = boundedLogText(value, 2000)
		case map[string]any:
			evidence.Type = boundedLogText(providerErrorValue(value["type"]), 200)
			evidence.Code = boundedLogText(providerErrorValue(value["code"]), 200)
			evidence.Message = boundedLogText(providerErrorValue(value["message"]), 2000)
			if evidence.Message == "" {
				evidence.Message = boundedLogText(providerErrorValue(value["detail"]), 2000)
			}
			if evidence.Message == "" {
				if safe, marshalErr := json.Marshal(Redact(value)); marshalErr == nil {
					evidence.Message = boundedLogText(string(safe), 2000)
				}
			}
		default:
			evidence.Message = boundedLogText(providerErrorValue(value), 2000)
		}
	}
	if evidence.Message == "" {
		evidence.Message = boundedLogText(fallback, 2000)
	}
	evidence.Retryable = providerStreamErrorRetryable(evidence)
	return providerStreamEventError{Evidence: evidence}
}

func providerErrorValue(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func providerStreamErrorRetryable(evidence ProviderStreamError) bool {
	value := strings.ToLower(strings.Join([]string{evidence.EventType, evidence.Type, evidence.Code, evidence.Message}, " "))
	for _, marker := range []string{
		"invalid_request", "authentication", "unauthorized", "forbidden", "permission", "invalid_api_key",
		"billing", "insufficient_quota", "rate_limit", "context_length", "max_output", "max_token", "content_policy",
	} {
		if strings.Contains(value, marker) {
			return false
		}
	}
	// Providers do not consistently attach a code to transient upstream SSE
	// failures. An otherwise unclassified error event gets one bounded retry.
	return true
}

var (
	errProviderNormalizedOutputTooLarge = errors.New("provider normalized stream output exceeds 8 MiB")
	errProviderSSEEventTooLarge         = errors.New("provider SSE event exceeds 1 MiB")
	errProviderOutputTruncated          = errors.New("provider output was truncated")
	errProviderOutputIncomplete         = errors.New("provider output did not complete normally")
)

func parseProviderCompletionStream(provider Provider, reader io.Reader, onEvent func()) (string, providerCompletionUsage, int, bool, error) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), (1<<20)+1)
	var output strings.Builder
	var usage providerCompletionUsage
	var dataLines []string
	eventBytes := 0
	var eventName string
	eventCount := 0
	completed := false
	writeOutput := func(value string) error {
		if len(value) > providerNormalizedOutputBytes-output.Len() {
			return errProviderNormalizedOutputTooLarge
		}
		_, _ = output.WriteString(value)
		return nil
	}
	flush := func() error {
		if len(dataLines) == 0 {
			eventName = ""
			return nil
		}
		payload := strings.Join(dataLines, "\n")
		dataLines = nil
		currentEventName := eventName
		eventName = ""
		eventBytes = 0
		if strings.TrimSpace(payload) == "[DONE]" {
			return nil
		}
		var event struct {
			Type    string          `json:"type"`
			Delta   string          `json:"delta"`
			Text    string          `json:"text"`
			Error   json.RawMessage `json:"error"`
			Choices []struct {
				Delta struct {
					Content string `json:"content"`
				} `json:"delta"`
				FinishReason string `json:"finish_reason"`
			} `json:"choices"`
			Usage struct {
				Prompt     int `json:"prompt_tokens"`
				Completion int `json:"completion_tokens"`
				Input      int `json:"input_tokens"`
				Output     int `json:"output_tokens"`
			} `json:"usage"`
			Response struct {
				Usage struct {
					Input  int `json:"input_tokens"`
					Output int `json:"output_tokens"`
				} `json:"usage"`
				Error             json.RawMessage `json:"error"`
				IncompleteDetails struct {
					Reason string `json:"reason"`
				} `json:"incomplete_details"`
			} `json:"response"`
			IncompleteDetails struct {
				Reason string `json:"reason"`
			} `json:"incomplete_details"`
		}
		if err := json.Unmarshal([]byte(payload), &event); err != nil {
			return fmt.Errorf("invalid provider stream event: %w", err)
		}
		if event.Type == "" {
			event.Type = currentEventName
		}
		eventCount++
		if onEvent != nil {
			onEvent()
		}
		if providerStreamErrorPresent(event.Error) {
			return decodeProviderStreamError(event.Type, event.Error, "provider returned an SSE error event")
		}
		if event.Type == "error" && provider.APIMode != ProviderAPIModeResponses {
			return decodeProviderStreamError(event.Type, json.RawMessage(payload), "provider returned an SSE error event")
		}
		if provider.APIMode == ProviderAPIModeResponses {
			switch event.Type {
			case "response.output_text.delta":
				if err := writeOutput(event.Delta); err != nil {
					return err
				}
			case "response.output_text.done":
				if output.Len() == 0 {
					if err := writeOutput(event.Text); err != nil {
						return err
					}
				}
			case "response.completed":
				completed = true
				usage = providerCompletionUsage{InputTokens: event.Response.Usage.Input, OutputTokens: event.Response.Usage.Output}
			case "error", "response.failed":
				raw := event.Response.Error
				if !providerStreamErrorPresent(raw) {
					raw = json.RawMessage(payload)
				}
				return decodeProviderStreamError(event.Type, raw, "provider response stream failed")
			case "response.incomplete":
				reason := event.Response.IncompleteDetails.Reason
				if reason == "" {
					reason = event.IncompleteDetails.Reason
				}
				if providerIndicatesOutputLimit(reason) {
					return fmt.Errorf("%w: %s", errProviderOutputTruncated, reason)
				}
				return fmt.Errorf("%w: %s", errProviderOutputIncomplete, reason)
			}
			return nil
		}
		for _, choice := range event.Choices {
			if err := writeOutput(choice.Delta.Content); err != nil {
				return err
			}
			switch strings.ToLower(strings.TrimSpace(choice.FinishReason)) {
			case "", "null":
			case "stop":
				completed = true
			case "length", "max_tokens", "max_output_tokens":
				return fmt.Errorf("%w: finish_reason=%s", errProviderOutputTruncated, choice.FinishReason)
			default:
				return fmt.Errorf("%w: finish_reason=%s", errProviderOutputIncomplete, choice.FinishReason)
			}
		}
		if event.Usage.Prompt != 0 || event.Usage.Completion != 0 {
			usage = providerCompletionUsage{InputTokens: event.Usage.Prompt, OutputTokens: event.Usage.Completion}
		}
		return nil
	}
	for scanner.Scan() {
		line := strings.TrimSuffix(scanner.Text(), "\r")
		if line == "" {
			if err := flush(); err != nil {
				return output.String(), usage, eventCount, completed, err
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			value := strings.TrimPrefix(line, "data:")
			value = strings.TrimPrefix(value, " ")
			additional := len(value)
			if len(dataLines) > 0 {
				additional++
			}
			if additional > providerSSEEventBytes-eventBytes {
				return output.String(), usage, eventCount, completed, errProviderSSEEventTooLarge
			}
			eventBytes += additional
			dataLines = append(dataLines, value)
		} else if strings.HasPrefix(line, "event:") {
			eventName = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		}
	}
	if err := scanner.Err(); err != nil {
		return output.String(), usage, eventCount, completed, err
	}
	if err := flush(); err != nil {
		return output.String(), usage, eventCount, completed, err
	}
	return output.String(), usage, eventCount, completed, nil
}

func providerIndicatesOutputLimit(value string) bool {
	value = strings.ToLower(strings.TrimSpace(value))
	return strings.Contains(value, "max_output") || strings.Contains(value, "max_token") || strings.Contains(value, "length")
}

func (s *DiagnosticService) recordProviderCall(evidence ProviderCallEvidence, errorCode string, cause error) {
	if s == nil || s.Store == nil {
		return
	}
	level := "info"
	message := "External LLM request completed"
	if cause != nil {
		level = "error"
		message = "External LLM request failed"
		if errorCode == "" {
			errorCode = "provider_request_failed"
		}
	}
	attributes, _ := json.Marshal(map[string]any{
		"action": evidence.Action,
		"error_detail": func() string {
			if cause == nil {
				return ""
			}
			detail, _ := Redact(cause.Error()).(string)
			if len(detail) > 4000 {
				return detail[:4000]
			}
			return detail
		}(),
		"external_request": map[string]any{
			"provider_id": evidence.ProviderID, "model": evidence.Model,
			"api_mode": evidence.APIMode, "reasoning_effort": evidence.ReasoningEffort,
			"method":        evidence.Method,
			"endpoint_path": evidence.EndpointPath, "timeout_seconds": evidence.TimeoutSeconds,
			"body": evidence.Request, "streaming": evidence.Streaming,
			"attempt": evidence.Attempt, "max_attempts": evidence.MaxAttempts,
		},
		"external_response": map[string]any{
			"status_code": evidence.StatusCode, "content_type": evidence.ContentType,
			"shape": evidence.ResponseShape, "body": evidence.Response,
			"response_headers_ms": evidence.ResponseHeadersMS, "first_event_ms": evidence.FirstEventMS,
			"last_event_ms": evidence.LastEventMS, "max_event_gap_ms": evidence.MaxEventGapMS,
			"stream_event_count": evidence.StreamEventCount, "stream_completed": evidence.StreamCompleted,
			"stream_error": evidence.StreamError,
		},
		"external_call_performed": evidence.ExternalCallPerformed,
	})
	persistCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	method := evidence.Method
	if method == "" {
		method = http.MethodPost
	}
	_, _ = s.Store.InsertLog(persistCtx, LogEntry{
		OccurredAt: evidence.PerformedAt, Level: level, Component: "external.llm", Message: message,
		RequestID: evidence.RequestID, TraceID: evidence.TraceID, Method: method,
		Route: evidence.EndpointPath, StatusCode: evidence.StatusCode, DurationMS: evidence.LatencyMS,
		ResponseBytes: int64(evidence.Response.BodyBytes), ErrorCode: errorCode, Attributes: attributes,
	})
}

func parseProviderCompletion(provider Provider, body []byte) (string, providerCompletionUsage, error) {
	if provider.APIMode != ProviderAPIModeResponses {
		var envelope struct {
			Choices []struct {
				Message struct {
					Content string `json:"content"`
				} `json:"message"`
				FinishReason string `json:"finish_reason"`
			} `json:"choices"`
			Usage struct {
				Prompt     int `json:"prompt_tokens"`
				Completion int `json:"completion_tokens"`
			} `json:"usage"`
		}
		if json.Unmarshal(body, &envelope) != nil || len(envelope.Choices) == 0 || strings.TrimSpace(envelope.Choices[0].Message.Content) == "" {
			return "", providerCompletionUsage{}, errors.New("provider response is missing a chat completion choice")
		}
		switch strings.ToLower(strings.TrimSpace(envelope.Choices[0].FinishReason)) {
		case "stop":
		case "length", "max_tokens", "max_output_tokens":
			return "", providerCompletionUsage{}, fmt.Errorf("%w: finish_reason=%s", errProviderOutputTruncated, envelope.Choices[0].FinishReason)
		default:
			return "", providerCompletionUsage{}, fmt.Errorf("%w: finish_reason=%s", errProviderOutputIncomplete, envelope.Choices[0].FinishReason)
		}
		return envelope.Choices[0].Message.Content, providerCompletionUsage{InputTokens: envelope.Usage.Prompt, OutputTokens: envelope.Usage.Completion}, nil
	}
	var envelope struct {
		Status     string `json:"status"`
		OutputText string `json:"output_text"`
		Output     []struct {
			Content []struct {
				Type string `json:"type"`
				Text string `json:"text"`
			} `json:"content"`
		} `json:"output"`
		Usage struct {
			Input  int `json:"input_tokens"`
			Output int `json:"output_tokens"`
		} `json:"usage"`
		IncompleteDetails struct {
			Reason string `json:"reason"`
		} `json:"incomplete_details"`
	}
	if json.Unmarshal(body, &envelope) != nil {
		return "", providerCompletionUsage{}, errors.New("provider response is not valid Responses JSON")
	}
	if strings.ToLower(strings.TrimSpace(envelope.Status)) != "completed" {
		if providerIndicatesOutputLimit(envelope.IncompleteDetails.Reason) {
			return "", providerCompletionUsage{}, fmt.Errorf("%w: status=%s reason=%s", errProviderOutputTruncated, envelope.Status, envelope.IncompleteDetails.Reason)
		}
		return "", providerCompletionUsage{}, fmt.Errorf("%w: status=%s", errProviderOutputIncomplete, envelope.Status)
	}
	content := strings.TrimSpace(envelope.OutputText)
	if content == "" {
		parts := []string{}
		for _, item := range envelope.Output {
			for _, part := range item.Content {
				if part.Type == "output_text" && strings.TrimSpace(part.Text) != "" {
					parts = append(parts, part.Text)
				}
			}
		}
		content = strings.TrimSpace(strings.Join(parts, "\n"))
	}
	if content == "" {
		return "", providerCompletionUsage{}, errors.New("provider response is missing Responses output text")
	}
	return content, providerCompletionUsage{InputTokens: envelope.Usage.Input, OutputTokens: envelope.Usage.Output}, nil
}
