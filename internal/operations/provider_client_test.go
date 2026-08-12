package operations

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return fn(request) }

func TestProviderRootBaseURLUsesStandardV1Prefix(t *testing.T) {
	endpoint, path, err := providerAPIEndpoint("https://models.example.invalid", "remote", "models")
	if err != nil {
		t.Fatal(err)
	}
	if endpoint != "https://models.example.invalid/v1/models" || path != "/v1/models" {
		t.Fatalf("endpoint/path = %q/%q", endpoint, path)
	}
	endpoint, path, err = providerAPIEndpoint("http://ollama:11434/v1/", "local", "responses")
	if err != nil || endpoint != "http://ollama:11434/v1/responses" || path != "/v1/responses" {
		t.Fatalf("existing prefix endpoint/path = %q/%q err=%v", endpoint, path, err)
	}
}

func TestProviderCatalogUsesReportedCapabilitiesWithoutInventingThem(t *testing.T) {
	body := []byte(`{"object":"list","data":[{"id":"plain"},{"id":"reasoner","capabilities":{"reasoning_efforts":["low","high","unsupported"]}}]}`)
	capabilities, shape, err := parseProviderCatalog(body)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(shape, ",") != "data:array,object:string" || len(capabilities.Models) != 2 {
		t.Fatalf("shape/capabilities = %#v/%#v", shape, capabilities)
	}
	if capabilities.Models[0].ID != "plain" || capabilities.Models[0].ReasoningSource != "unreported" || len(capabilities.Models[0].ReasoningEfforts) != 0 {
		t.Fatalf("unreported model capabilities = %#v", capabilities.Models[0])
	}
	if got := capabilities.Models[1]; got.ReasoningSource != "provider_reported" || strings.Join(got.ReasoningEfforts, ",") != "low,high" {
		t.Fatalf("reported model capabilities = %#v", got)
	}
}

func TestProviderCompletionUsesConfiguredProtocolAndReasoningShape(t *testing.T) {
	provider := Provider{BaseURL: "https://models.example.invalid", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeResponses, ReasoningEffort: "high"}
	endpoint, path, body, err := buildProviderCompletionRequest(provider, "system", "user", true, 32)
	if err != nil {
		t.Fatal(err)
	}
	if endpoint != "https://models.example.invalid/v1/responses" || path != "/v1/responses" {
		t.Fatalf("endpoint/path = %q/%q", endpoint, path)
	}
	var request map[string]any
	if json.Unmarshal(body, &request) != nil || request["reasoning"].(map[string]any)["effort"] != "high" || request["reasoning_effort"] != nil || request["messages"] != nil {
		t.Fatalf("Responses request = %#v", request)
	}
	provider.APIMode = ProviderAPIModeChatCompletions
	_, path, body, err = buildProviderCompletionRequest(provider, "system", "user", false, 32)
	if err != nil {
		t.Fatal(err)
	}
	request = nil
	if json.Unmarshal(body, &request) != nil || request["reasoning_effort"] != "high" || request["reasoning"] != nil || path != "/v1/chat/completions" {
		t.Fatalf("Chat Completions request = %#v path=%q", request, path)
	}
}

func TestProviderStreamingRequestUsesSSEAndUsage(t *testing.T) {
	provider := Provider{BaseURL: "https://models.example.invalid", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, ReasoningEffort: "high"}
	_, path, body, err := buildProviderStreamingCompletionRequest(provider, "system", "user", true, 32)
	if err != nil {
		t.Fatal(err)
	}
	var request map[string]any
	if json.Unmarshal(body, &request) != nil || request["stream"] != true || path != "/v1/chat/completions" {
		t.Fatalf("streaming Chat Completions request = %#v path=%q", request, path)
	}
	options, ok := request["stream_options"].(map[string]any)
	if !ok || options["include_usage"] != true {
		t.Fatalf("stream options = %#v", request["stream_options"])
	}

	provider.APIMode = ProviderAPIModeResponses
	_, path, body, err = buildProviderStreamingCompletionRequest(provider, "system", "user", true, 32)
	if err != nil {
		t.Fatal(err)
	}
	request = nil
	if json.Unmarshal(body, &request) != nil || request["stream"] != true || request["stream_options"] != nil || path != "/v1/responses" {
		t.Fatalf("streaming Responses request = %#v path=%q", request, path)
	}
}

func TestParseProviderChatCompletionStream(t *testing.T) {
	body := strings.Join([]string{
		`data: {"id":"chatcmpl-1","choices":[{"delta":{"role":"assistant","content":"{\"ok\":"}}]}`,
		"",
		`data: {"id":"chatcmpl-1","choices":[{"delta":{"reasoning_content":"private"}}]}`,
		"",
		`data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"true}"},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	content, usage, events, completed, err := parseProviderCompletionStream(Provider{APIMode: ProviderAPIModeChatCompletions}, strings.NewReader(body), nil)
	if err != nil || content != `{"ok":true}` || usage.InputTokens != 4 || usage.OutputTokens != 2 || events != 3 || !completed {
		t.Fatalf("content/usage/events/completed = %q/%#v/%d/%t err=%v", content, usage, events, completed, err)
	}
}

func TestParseProviderResponsesStream(t *testing.T) {
	body := strings.Join([]string{
		"event: response.created",
		`data: {"type":"response.created","response":{"id":"resp_1"}}`,
		"",
		"event: response.output_text.delta",
		`data: {"type":"response.output_text.delta","delta":"{\"ok\":true}"}`,
		"",
		"event: response.completed",
		`data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":3}}}`,
		"",
	}, "\n")
	content, usage, events, completed, err := parseProviderCompletionStream(Provider{APIMode: ProviderAPIModeResponses}, strings.NewReader(body), nil)
	if err != nil || content != `{"ok":true}` || usage.InputTokens != 5 || usage.OutputTokens != 3 || events != 3 || !completed {
		t.Fatalf("content/usage/events/completed = %q/%#v/%d/%t err=%v", content, usage, events, completed, err)
	}
}

func TestParseProviderStreamReturnsSafeUpstreamError(t *testing.T) {
	body := strings.Join([]string{
		`data: {"choices":[{"delta":{"reasoning_content":"private-chain"}}]}`,
		"",
		`data: {"type":"error","error":{"message":"temporary upstream failure api_key=must-not-persist","type":"server_error","code":"upstream_timeout"}}`,
		"",
	}, "\n")
	content, _, events, completed, err := parseProviderCompletionStream(Provider{APIMode: ProviderAPIModeChatCompletions}, strings.NewReader(body), nil)
	var streamErr providerStreamEventError
	if !errors.As(err, &streamErr) || content != "" || events != 2 || completed {
		t.Fatalf("content/events/completed/error = %q/%d/%t/%v", content, events, completed, err)
	}
	if streamErr.Evidence.Code != "upstream_timeout" || streamErr.Evidence.Type != "server_error" || !streamErr.Evidence.Retryable {
		t.Fatalf("stream error evidence = %#v", streamErr.Evidence)
	}
	if strings.Contains(streamErr.Evidence.Message, "must-not-persist") || !strings.Contains(streamErr.Evidence.Message, RedactedValue) {
		t.Fatalf("stream error was not redacted: %q", streamErr.Evidence.Message)
	}
}

func TestParseProviderStreamIgnoresEmptyErrorEnvelope(t *testing.T) {
	body := "data: {\"error\":{},\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
	content, _, _, completed, err := parseProviderCompletionStream(Provider{APIMode: ProviderAPIModeChatCompletions}, strings.NewReader(body), nil)
	if err != nil || content != "ok" || !completed {
		t.Fatalf("content/completed/error = %q/%t/%v", content, completed, err)
	}
}

func TestStreamingCompletionRecordsTransportMetrics(t *testing.T) {
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		requestBody, err := io.ReadAll(request.Body)
		if err != nil {
			return nil, err
		}
		var payload map[string]any
		if json.Unmarshal(requestBody, &payload) != nil || payload["stream"] != true || request.Header.Get("Accept") != "text/event-stream" {
			t.Fatalf("stream request payload/accept = %#v/%q", payload, request.Header.Get("Accept"))
		}
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}
	content, _, metrics, err := service.doProviderStreamingCompletion(context.Background(), provider, "stream_test", "system", "user", false)
	if err != nil || content != "ok" || metrics.EventCount != 1 || !metrics.Completed || metrics.ResponseHeadersMS < 0 || metrics.TotalLatencyMS < metrics.ResponseHeadersMS {
		t.Fatalf("content/metrics = %q/%#v err=%v", content, metrics, err)
	}
}

func TestStreamingCompletionAllowsMoreThanOneMiBOfReasoningEvents(t *testing.T) {
	var body strings.Builder
	reasoningEvent := `data: {"choices":[{"delta":{"reasoning_content":"` + strings.Repeat("r", 256) + `"}}]}` + "\n\n"
	for range 5000 {
		body.WriteString(reasoningEvent)
	}
	body.WriteString("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n")
	if body.Len() <= 1<<20 {
		t.Fatalf("fixture stream is only %d bytes", body.Len())
	}
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(body.String()))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}
	content, _, metrics, err := service.doProviderStreamingCompletion(context.Background(), provider, "stream_test", "system", "user", false)
	if err != nil || content != "ok" || metrics.EventCount != 5001 || !metrics.Completed {
		t.Fatalf("content/metrics = %q/%#v err=%v", content, metrics, err)
	}
}

func TestStreamingCompletionAllowsNormalizedOutputAboveOneMiB(t *testing.T) {
	chunk := strings.Repeat("x", 256<<10)
	event := `data: {"choices":[{"delta":{"content":"` + chunk + `"}}]}` + "\n\n"
	body := strings.Repeat(event, 5) + "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}
	content, _, metrics, err := service.doProviderStreamingCompletion(context.Background(), provider, "stream_test", "system", "user", false)
	if err != nil || len(content) != 5*(256<<10) || !metrics.Completed {
		t.Fatalf("content bytes/metrics = %d/%#v err=%v", len(content), metrics, err)
	}
}

func TestStreamingCompletionStillBoundsNormalizedOutput(t *testing.T) {
	chunk := strings.Repeat("x", 512<<10)
	event := `data: {"choices":[{"delta":{"content":"` + chunk + `"}}]}` + "\n\n"
	body := strings.Repeat(event, 17)
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}
	_, _, _, err := service.doProviderStreamingCompletion(context.Background(), provider, "stream_test", "system", "user", false)
	if err == nil || providerCallErrorCode(err) != "provider_response_too_large" || !errors.Is(err, errProviderNormalizedOutputTooLarge) {
		t.Fatalf("stream output limit error = %v", err)
	}
}

func TestConfiguredCompletionUsesProviderStreamingSwitch(t *testing.T) {
	var streamed bool
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			return nil, err
		}
		streamed, _ = payload["stream"].(bool)
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60, StreamingEnabled: true}
	content, _, _, metrics, err := service.doConfiguredProviderCompletion(context.Background(), provider, "configured_stream", "system", "user", false)
	if err != nil || !streamed || content != "ok" || metrics == nil || !metrics.Completed {
		t.Fatalf("configured stream content/streamed/metrics = %q/%t/%#v err=%v", content, streamed, metrics, err)
	}
}

func TestConfiguredCompletionRetriesTransientStreamErrorWithinBudget(t *testing.T) {
	calls := 0
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		calls++
		var payload map[string]any
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			return nil, err
		}
		if payload["max_completion_tokens"] != float64(8192) {
			t.Fatalf("max_completion_tokens attempt %d = %#v", calls, payload["max_completion_tokens"])
		}
		body := `data: {"type":"error","error":{"message":"temporary upstream failure","type":"server_error","code":"upstream_timeout"}}` + "\n\n"
		if calls == 2 {
			body = "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
		}
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60, StreamingEnabled: true}
	content, _, _, metrics, err := service.doConfiguredProviderCompletionWithOptions(context.Background(), provider, "configured_stream", "system", "user", false, providerCompletionOptions{MaxOutputTokens: 8192, RetryTransientFailure: true})
	if err != nil || content != "ok" || calls != 2 || metrics == nil || metrics.AttemptCount != 2 || !metrics.RecoveredByRetry || !metrics.Completed {
		t.Fatalf("content/calls/metrics = %q/%d/%#v err=%v", content, calls, metrics, err)
	}
}

func TestConfiguredCompletionRetriesHTTP524BeforeStreamStarts(t *testing.T) {
	calls := 0
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		status := http.StatusOK
		contentType := "text/event-stream"
		body := "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
		if calls == 1 {
			status = 524
			contentType = "text/html"
			body = "<html><title>524: A timeout occurred</title></html>"
		}
		return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": []string{contentType}}, Body: io.NopCloser(strings.NewReader(body))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60, StreamingEnabled: true}
	content, _, _, metrics, err := service.doConfiguredProviderCompletion(context.Background(), provider, "configured_stream", "system", "user", false)
	if err != nil || content != "ok" || calls != 2 || metrics == nil || metrics.AttemptCount != 2 || !metrics.RecoveredByRetry || !metrics.Completed {
		t.Fatalf("content/calls/metrics = %q/%d/%#v err=%v", content, calls, metrics, err)
	}
}

func TestConfiguredCompletionDoesNotRetryPermanentStreamError(t *testing.T) {
	calls := 0
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		body := `data: {"type":"error","error":{"message":"invalid credential","type":"authentication_error","code":"invalid_api_key"}}` + "\n\n"
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60, StreamingEnabled: true}
	_, _, _, _, err := service.doConfiguredProviderCompletion(context.Background(), provider, "configured_stream", "system", "user", false)
	if err == nil || providerCallErrorCode(err) != "provider_stream_error" || calls != 1 {
		t.Fatalf("calls/error = %d/%v", calls, err)
	}
	failure, ok := DescribeProviderCallFailure(err)
	if !ok || failure.Evidence.StreamError == nil || failure.Evidence.StreamError.Code != "invalid_api_key" || failure.Evidence.StreamError.Retryable {
		t.Fatalf("provider failure = %#v, ok=%t", failure, ok)
	}
}

func TestParseResponsesOutputText(t *testing.T) {
	content, usage, err := parseProviderCompletion(Provider{APIMode: ProviderAPIModeResponses}, []byte(`{"status":"completed","output":[{"content":[{"type":"output_text","text":"{\"ok\":true}"}]}],"usage":{"input_tokens":4,"output_tokens":2}}`))
	if err != nil || content != `{"ok":true}` || usage.InputTokens != 4 || usage.OutputTokens != 2 {
		t.Fatalf("content/usage = %q/%#v err=%v", content, usage, err)
	}
}

func TestProviderCompletionClassifiesTimeoutWithSafeEvidence(t *testing.T) {
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, context.DeadlineExceeded
	})}}
	ctx := WithCorrelation(context.Background(), Correlation{RequestID: "request-fixture", TraceID: "11111111-1111-4111-8111-111111111111"})
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}

	_, _, _, err := service.doProviderCompletion(ctx, provider, "test_completion", "system", "user", true)
	if err == nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("completion error = %v", err)
	}
	failure, ok := DescribeProviderCallFailure(err)
	if !ok || failure.Code != "provider_timeout" || failure.Detail != "provider request timed out after 60 seconds" {
		t.Fatalf("provider failure = %#v, ok=%t", failure, ok)
	}
	if failure.Evidence.EndpointPath != "/v1/chat/completions" || !failure.Evidence.ExternalCallPerformed || failure.Evidence.RequestID != "request-fixture" || failure.Evidence.TraceID == "" {
		t.Fatalf("provider failure evidence = %#v", failure.Evidence)
	}
}

func TestProviderPayloadEvidenceRedactsAndBoundsPreview(t *testing.T) {
	evidence := providerPayloadEvidence([]byte(`{"model":"fixture","api_key":"must-not-persist","messages":[{"content":"token=also-secret"}]}`))
	if strings.Contains(evidence.Preview, "must-not-persist") || strings.Contains(evidence.Preview, "also-secret") {
		t.Fatalf("payload preview was not redacted: %s", evidence.Preview)
	}
	if evidence.BodySHA256 == "" || evidence.BodyBytes == 0 || evidence.PreviewTruncated {
		t.Fatalf("unexpected payload evidence: %#v", evidence)
	}

	large := providerPayloadEvidence([]byte(strings.Repeat("x", providerPayloadPreviewBytes+257)))
	if !large.PreviewTruncated || large.PreviewOmittedBytes != 257 || large.PreviewBytes != providerPayloadPreviewBytes {
		t.Fatalf("large payload evidence = %#v", large)
	}
	incomplete := incompleteProviderPayloadEvidence([]byte("captured prefix"))
	if incomplete.BodyComplete || incomplete.BodySHA256 != "" || incomplete.CapturedSHA256 == "" {
		t.Fatalf("incomplete payload evidence = %#v", incomplete)
	}
}

func TestProviderRequestEvidenceNeverPersistsPromptContent(t *testing.T) {
	const privateRule = "PRIVATE_TRACKER_RULE_SENTINEL 100MB/s"
	_, _, body, err := buildProviderStreamingCompletionRequest(Provider{
		BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner",
		APIMode: ProviderAPIModeChatCompletions,
	}, "private system prompt", privateRule, true, 8192)
	if err != nil {
		t.Fatal(err)
	}
	evidence := providerRequestPayloadEvidence(body)
	if strings.Contains(evidence.Preview, privateRule) || strings.Contains(evidence.Preview, "private system prompt") || strings.Contains(evidence.Preview, `"content":`) {
		t.Fatalf("request evidence retained prompt content: %s", evidence.Preview)
	}
	if evidence.PreviewKind != "request_metadata" || evidence.BodySHA256 == "" || !strings.Contains(evidence.Preview, "content_bytes") {
		t.Fatalf("request metadata evidence = %#v", evidence)
	}
}

func TestProviderContractFingerprintBindsExecutionConfiguration(t *testing.T) {
	base := Provider{
		ID: "11111111-1111-4111-8111-111111111111", BaseURL: "https://models.example.invalid/v1", Model: "reasoner",
		DataLevel: "remote", APIMode: ProviderAPIModeResponses, ReasoningEffort: "high",
		UseCases: []string{ProviderUseCaseRuleAnalysis, ProviderUseCaseIncidentDiagnosis}, JSONMode: true,
		StreamingEnabled: true, Enabled: true, OutboundConsent: true, TimeoutSeconds: 600, secretID: "credential-version-a",
	}
	first := providerContractFingerprint(base)
	reordered := base
	reordered.UseCases = []string{ProviderUseCaseIncidentDiagnosis, ProviderUseCaseRuleAnalysis}
	if got := providerContractFingerprint(reordered); got != first {
		t.Fatalf("use-case ordering changed fingerprint: %s != %s", got, first)
	}
	for label, mutate := range map[string]func(*Provider){
		"endpoint":   func(value *Provider) { value.BaseURL = "https://other.example.invalid/v1" },
		"model":      func(value *Provider) { value.Model = "other" },
		"protocol":   func(value *Provider) { value.APIMode = ProviderAPIModeChatCompletions },
		"boundary":   func(value *Provider) { value.DataLevel = "local" },
		"credential": func(value *Provider) { value.secretID = "credential-version-b" },
		"streaming":  func(value *Provider) { value.StreamingEnabled = false },
		"timeout":    func(value *Provider) { value.TimeoutSeconds = 599 },
	} {
		changed := base
		mutate(&changed)
		if got := providerContractFingerprint(changed); got == first {
			t.Fatalf("%s did not change provider contract fingerprint", label)
		}
	}
}

func TestProviderCompletionRejectsTruncatedAndIncompleteStatuses(t *testing.T) {
	_, _, err := parseProviderCompletion(Provider{APIMode: ProviderAPIModeChatCompletions}, []byte(`{"choices":[{"message":{"content":"{\"ok\":"},"finish_reason":"length"}]}`))
	if !errors.Is(err, errProviderOutputTruncated) {
		t.Fatalf("buffered length finish error = %v", err)
	}
	stream := "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"},\"finish_reason\":\"length\"}]}\n\ndata: [DONE]\n\n"
	_, _, _, completed, err := parseProviderCompletionStream(Provider{APIMode: ProviderAPIModeChatCompletions}, strings.NewReader(stream), nil)
	if !errors.Is(err, errProviderOutputTruncated) || completed {
		t.Fatalf("stream length finish completed/error = %t/%v", completed, err)
	}
	_, _, err = parseProviderCompletion(Provider{APIMode: ProviderAPIModeResponses}, []byte(`{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output_text":"partial"}`))
	if !errors.Is(err, errProviderOutputTruncated) {
		t.Fatalf("Responses incomplete error = %v", err)
	}
}

func TestProviderStreamBoundsAggregateSSEEvent(t *testing.T) {
	line := strings.Repeat("x", 600<<10)
	body := "data: " + line + "\ndata: " + line + "\n\n"
	_, _, _, _, err := parseProviderCompletionStream(Provider{APIMode: ProviderAPIModeChatCompletions}, strings.NewReader(body), nil)
	if !errors.Is(err, errProviderSSEEventTooLarge) {
		t.Fatalf("aggregate SSE event error = %v", err)
	}
}

func TestStreamingCompletionMarksOversizedNonSSEBodyIncomplete(t *testing.T) {
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(strings.Repeat("x", providerBufferedResponseBytes+1)))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}
	_, _, _, err := service.doProviderStreamingCompletion(context.Background(), provider, "stream_test", "system", "user", false)
	failure, ok := DescribeProviderCallFailure(err)
	if !ok || failure.Code != "provider_response_too_large" || failure.Evidence.Response.BodyComplete || failure.Evidence.Response.BodySHA256 != "" || failure.Evidence.Response.CapturedSHA256 == "" {
		t.Fatalf("oversized non-SSE failure = %#v, err=%v", failure, err)
	}
}

func TestProviderStreamingEvidenceOmitsReasoningEvents(t *testing.T) {
	raw := []byte(`data: {"choices":[{"delta":{"reasoning_content":"private-chain"}}]}`)
	evidence := providerStreamingPayloadEvidence(raw, `{"ok":true}`, providerCompletionUsage{InputTokens: 4, OutputTokens: 2}, true)
	if strings.Contains(evidence.Preview, "private-chain") || strings.Contains(evidence.Preview, "reasoning_content") {
		t.Fatalf("stream preview retained reasoning: %s", evidence.Preview)
	}
	if evidence.BodyBytes != len(raw) || evidence.BodySHA256 == "" || evidence.PreviewKind != "normalized_stream_output" || strings.Contains(evidence.Preview, `{"ok":true}`) || !strings.Contains(evidence.Preview, `"output_bytes":11`) || !strings.Contains(evidence.Preview, `"output_sha256"`) {
		t.Fatalf("stream evidence = %#v", evidence)
	}
}

func TestStreamingCompletionDoesNotMarkIncompleteStreamBodyComplete(t *testing.T) {
	service := &DiagnosticService{Client: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader("data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n"))}, nil
	})}}
	provider := Provider{BaseURL: "https://models.example.invalid/v1", DataLevel: "remote", Model: "reasoner", APIMode: ProviderAPIModeChatCompletions, TimeoutSeconds: 60}
	_, _, _, err := service.doProviderStreamingCompletion(context.Background(), provider, "stream_test", "system", "user", false)
	failure, ok := DescribeProviderCallFailure(err)
	if !ok || failure.Code != "provider_stream_incomplete" || failure.Evidence.ResponseSHA256 != "" || failure.Evidence.Response.BodyComplete || failure.Evidence.Response.BodySHA256 != "" || failure.Evidence.Response.CapturedSHA256 == "" {
		t.Fatalf("incomplete stream evidence = %#v, err=%v", failure, err)
	}
}
