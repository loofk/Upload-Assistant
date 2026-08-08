package apiclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"
)

const maxResponseBytes = 16 << 20

var ErrValidation = errors.New("API client configuration is invalid")

type Error struct {
	HTTPStatus int
	Code       string
	Detail     string
}

func (e *Error) Error() string {
	if e.Code == "" {
		return fmt.Sprintf("Upload Assistant API returned HTTP %d", e.HTTPStatus)
	}
	return fmt.Sprintf("Upload Assistant API returned %s (HTTP %d): %s", e.Code, e.HTTPStatus, e.Detail)
}

type Client struct {
	base  *url.URL
	token string
	http  *http.Client
}

func New(baseURL, token string, timeout time.Duration, allowInsecureHTTP bool, transport http.RoundTripper) (*Client, error) {
	base, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil || base.Scheme == "" || base.Host == "" {
		return nil, fmt.Errorf("%w: api URL must be an absolute HTTP(S) URL", ErrValidation)
	}
	if base.Scheme != "http" && base.Scheme != "https" {
		return nil, fmt.Errorf("%w: api URL scheme must be http or https", ErrValidation)
	}
	if base.User != nil || base.RawQuery != "" || base.Fragment != "" {
		return nil, fmt.Errorf("%w: api URL must not contain credentials, query parameters, or fragments", ErrValidation)
	}
	if base.Scheme == "http" && !allowInsecureHTTP && !isLoopbackHost(base.Hostname()) {
		return nil, fmt.Errorf("%w: plaintext HTTP is restricted to loopback; use HTTPS or explicitly allow insecure HTTP", ErrValidation)
	}
	base.Path = strings.TrimRight(base.Path, "/")
	if timeout <= 0 || timeout > 10*time.Minute {
		return nil, fmt.Errorf("%w: timeout must be between 1ns and 10m", ErrValidation)
	}
	if transport == nil {
		transport = http.DefaultTransport
	}
	return &Client{
		base:  base,
		token: strings.TrimSpace(token),
		http: &http.Client{
			Timeout:   timeout,
			Transport: transport,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return errors.New("redirects are disabled")
			},
		},
	}, nil
}

func (c *Client) DoJSON(ctx context.Context, method, requestPath string, query url.Values, input any, headers map[string]string, authenticated bool) (json.RawMessage, error) {
	if c == nil || c.base == nil {
		return nil, fmt.Errorf("%w: client is not initialized", ErrValidation)
	}
	if !strings.HasPrefix(requestPath, "/") || strings.Contains(requestPath, "\\") {
		return nil, fmt.Errorf("%w: request path must be absolute", ErrValidation)
	}
	if authenticated && c.token == "" {
		return nil, fmt.Errorf("%w: API token is required", ErrValidation)
	}
	endpoint := *c.base
	endpoint.Path = path.Join(c.base.Path, requestPath)
	endpoint.RawQuery = query.Encode()
	var body io.Reader
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return nil, fmt.Errorf("encode API request: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint.String(), body)
	if err != nil {
		return nil, fmt.Errorf("build API request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	if input != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	for name, value := range headers {
		if strings.EqualFold(name, "Authorization") {
			return nil, fmt.Errorf("%w: authorization header cannot be overridden", ErrValidation)
		}
		request.Header.Set(name, value)
	}
	if authenticated {
		request.Header.Set("Authorization", "Bearer "+c.token)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, fmt.Errorf("call Upload Assistant API: %w", err)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read Upload Assistant API response: %w", err)
	}
	if len(responseBody) > maxResponseBytes {
		return nil, fmt.Errorf("Upload Assistant API response exceeds %d bytes", maxResponseBytes)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, decodeError(response.StatusCode, responseBody)
	}
	if !json.Valid(responseBody) {
		return nil, fmt.Errorf("Upload Assistant API returned a non-JSON response")
	}
	return json.RawMessage(responseBody), nil
}

func decodeError(status int, body []byte) error {
	var problem struct {
		Code   string `json:"code"`
		Title  string `json:"title"`
		Detail string `json:"detail"`
		Error  struct {
			Code   string `json:"code"`
			Detail string `json:"detail"`
		} `json:"error"`
		Blockers []struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"blockers"`
	}
	_ = json.Unmarshal(body, &problem)
	if problem.Code == "" {
		problem.Code = problem.Error.Code
	}
	if problem.Detail == "" {
		problem.Detail = problem.Error.Detail
	}
	if problem.Code == "" && len(problem.Blockers) > 0 {
		problem.Code = problem.Blockers[0].Code
	}
	if problem.Detail == "" && len(problem.Blockers) > 0 {
		problem.Detail = problem.Blockers[0].Message
	}
	problem.Code = clean(problem.Code, 100)
	problem.Detail = clean(problem.Detail, 500)
	if problem.Detail == "" {
		problem.Detail = clean(problem.Title, 500)
	}
	if problem.Detail == "" {
		problem.Detail = http.StatusText(status)
	}
	return &Error{HTTPStatus: status, Code: problem.Code, Detail: problem.Detail}
}

func clean(value string, limit int) string {
	value = strings.Map(func(character rune) rune {
		if character == '\r' || character == '\n' || character == '\x00' {
			return ' '
		}
		return character
	}, strings.TrimSpace(value))
	if len(value) > limit {
		value = value[:limit]
	}
	return value
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
