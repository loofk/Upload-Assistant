package apiclient

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestClientSendsBearerAndIdempotencyKey(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer ua_fixture_token_value_that_is_long_enough" {
			t.Fatalf("authorization header was not supplied")
		}
		if request.Header.Get("Idempotency-Key") != "fixture-key" || request.URL.Path != "/api/v2/jobs" {
			t.Fatalf("request = %s headers=%v", request.URL.String(), request.Header)
		}
		body, _ := io.ReadAll(request.Body)
		if !strings.Contains(string(body), `"kind":"retorrent"`) {
			t.Fatalf("body = %s", body)
		}
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{"ok":true,"status":"queued"}`))
	}))
	defer server.Close()
	client, err := New(server.URL, "ua_fixture_token_value_that_is_long_enough", time.Second, false, server.Client().Transport)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.DoJSON(context.Background(), http.MethodPost, "/api/v2/jobs", nil, map[string]any{"kind": "retorrent"}, map[string]string{"Idempotency-Key": "fixture-key"}, true)
	if err != nil || !strings.Contains(string(result), `"ok":true`) {
		t.Fatalf("result/error = %s/%v", result, err)
	}
}

func TestClientRejectsRemotePlaintextAndSanitizesProblem(t *testing.T) {
	if _, err := New("http://example.com", "", time.Second, false, nil); err == nil {
		t.Fatal("remote plaintext endpoint must be rejected")
	}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusConflict)
		_, _ = response.Write([]byte(`{"code":"rule_gate","detail":"review required\nnow"}`))
	}))
	defer server.Close()
	client, err := New(server.URL, "ua_fixture_token_value_that_is_long_enough", time.Second, false, server.Client().Transport)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.DoJSON(context.Background(), http.MethodGet, "/api/v2/jobs", nil, nil, nil, true)
	var apiError *Error
	if !errors.As(err, &apiError) || apiError.Code != "rule_gate" || strings.Contains(apiError.Detail, "\n") {
		t.Fatalf("error = %#v", err)
	}
}
