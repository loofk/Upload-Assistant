package rulecollector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestNormalizeSourcesPreservesOrderAndRejectsCredentialURLs(t *testing.T) {
	sources, err := normalizeSources([]SourceInput{
		{ID: "rules", URL: "https://tracker.example.invalid/rules.php", Scope: "完整规则"},
		{ID: "titles", URL: "https://wiki.example.invalid/upload-title-rules", Scope: "标题规范"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(sources) != 2 || sources[0].ID != "rules" || sources[1].ID != "titles" {
		t.Fatalf("source order = %#v", sources)
	}
	if sources[0].AuthMode != SourceAuthSiteCookie {
		t.Fatalf("legacy source auth mode = %q", sources[0].AuthMode)
	}
	publicSources, err := normalizeSources([]SourceInput{{URL: "https://wiki.example.invalid/rules", Scope: "公开规则", AuthMode: SourceAuthNone}})
	if err != nil || publicSources[0].AuthMode != SourceAuthNone || sourcesRequireCookie(publicSources) {
		t.Fatalf("public source normalization = %#v err=%v", publicSources, err)
	}
	for _, value := range []string{
		"http://tracker.example.invalid/rules", "https://user:secret@tracker.example.invalid/rules",
		"https://tracker.example.invalid/rules?passkey=secret", "https://tracker.example.invalid/rules#token",
	} {
		_, err := normalizeSources([]SourceInput{{URL: value, Scope: "规则"}})
		if !errors.Is(err, ErrInvalid) {
			t.Fatalf("URL %q error = %v", value, err)
		}
	}
	if _, err := normalizeSources([]SourceInput{{URL: "https://wiki.example.invalid/rules", Scope: "规则", AuthMode: "api_key"}}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("unsupported auth mode error = %v", err)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestFetchSendsCookieOnlyForExplicitCookieSource(t *testing.T) {
	for _, test := range []struct {
		name       string
		authMode   string
		wantCookie bool
	}{{name: "public", authMode: SourceAuthNone}, {name: "cookie", authMode: SourceAuthSiteCookie, wantCookie: true}} {
		t.Run(test.name, func(t *testing.T) {
			service := &Service{now: time.Now, httpClient: &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
				if got := request.Header.Get("Cookie"); (got != "") != test.wantCookie {
					t.Fatalf("Cookie header = %q, want present=%t", got, test.wantCookie)
				}
				if test.wantCookie && request.Header.Get("Cookie") != "session=synthetic" {
					t.Fatal("Cookie header was not normalized")
				}
				if !strings.HasPrefix(request.Header.Get("User-Agent"), "Mozilla/5.0") {
					t.Fatalf("User-Agent = %q", request.Header.Get("User-Agent"))
				}
				if request.Header.Get("Authorization") != "" || request.Header.Get("X-Api-Key") != "" {
					t.Fatal("rule source request received a runtime API credential header")
				}
				return &http.Response{StatusCode: http.StatusUnauthorized, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("denied")), Request: request}, nil
			})}}
			_, err := service.fetch(context.Background(), "run", CollectionDocument{URL: "https://wiki.example.invalid/rules", AuthMode: test.authMode}, "session=synthetic")
			if err == nil {
				t.Fatal("expected synthetic HTTP 401")
			}
		})
	}
}

func TestNormalizeCookieHeaderAcceptsBrowserAndJSONForms(t *testing.T) {
	for _, test := range []struct {
		name  string
		input string
		want  string
	}{
		{name: "browser header", input: "Cookie: session=synthetic; tracker=fixture;", want: "session=synthetic; tracker=fixture"},
		{name: "JSON object", input: `{"tracker":"fixture","session":"synthetic"}`, want: "session=synthetic; tracker=fixture"},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := normalizeCookieHeader(test.input)
			if err != nil || got != test.want {
				t.Fatalf("normalizeCookieHeader() = %q, %v; want %q", got, err, test.want)
			}
		})
	}
	for _, invalid := range []string{"", "session", "session=synthetic\r\nX-Test: value", `{"bad name":"value"}`, `{"session":"bad;value"}`} {
		if _, err := normalizeCookieHeader(invalid); err == nil {
			t.Fatalf("normalizeCookieHeader(%q) unexpectedly succeeded", invalid)
		}
	}
}

func TestSafeHTTPClientExtendsTLSHandshakeBudget(t *testing.T) {
	client := safeHTTPClient(45 * time.Second)
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("transport type = %T", client.Transport)
	}
	if transport.Proxy != nil {
		t.Fatal("rule collector must not inherit an ambient proxy")
	}
	if transport.TLSHandshakeTimeout != ruleSourceTLSHandshakeTimeout {
		t.Fatalf("TLS handshake timeout = %s", transport.TLSHandshakeTimeout)
	}
	if ruleSourceConnectTimeout != 20*time.Second {
		t.Fatalf("connect timeout = %s", ruleSourceConnectTimeout)
	}
	if transport.DialTLSContext == nil {
		t.Fatal("TLS dialer must validate and retry resolved public addresses")
	}
}

func TestDialResolvedPublicHostFallsBackAndRejectsPrivateAnswers(t *testing.T) {
	lookup := func(context.Context, string) ([]net.IPAddr, error) {
		return []net.IPAddr{{IP: net.ParseIP("8.8.8.8")}, {IP: net.ParseIP("1.1.1.1")}}, nil
	}
	var attempts []string
	connection, err := dialResolvedPublicHost(context.Background(), "tcp", "tracker.example.invalid:443", lookup,
		func(_ context.Context, _, address, serverName string) (net.Conn, error) {
			attempts = append(attempts, address)
			if serverName != "tracker.example.invalid" {
				t.Fatalf("server name = %q", serverName)
			}
			if len(attempts) == 1 {
				return nil, fmt.Errorf("synthetic TLS failure")
			}
			client, server := net.Pipe()
			_ = server.Close()
			return client, nil
		})
	if err != nil {
		t.Fatal(err)
	}
	_ = connection.Close()
	if len(attempts) != 2 {
		t.Fatalf("attempts = %v", attempts)
	}

	dialCalled := false
	_, err = dialResolvedPublicHost(context.Background(), "tcp", "tracker.example.invalid:443",
		func(context.Context, string) ([]net.IPAddr, error) {
			return []net.IPAddr{{IP: net.ParseIP("8.8.8.8")}, {IP: net.ParseIP("127.0.0.1")}}, nil
		}, func(context.Context, string, string, string) (net.Conn, error) {
			dialCalled = true
			return nil, nil
		})
	if err == nil || dialCalled {
		t.Fatalf("mixed private answer error=%v dial_called=%t", err, dialCalled)
	}
}

func TestClassifyFetchFailureSeparatesTLSAndDNS(t *testing.T) {
	if code, _ := classifyFetchFailure(&ruleSourceNetworkError{Phase: "tls", Err: context.DeadlineExceeded}); code != "rule_source_tls_timeout" {
		t.Fatalf("TLS timeout code = %q", code)
	}
	if code, _ := classifyFetchFailure(&ruleSourceNetworkError{Phase: "connect", Err: context.DeadlineExceeded}); code != "rule_source_connection_timeout" {
		t.Fatalf("connection timeout code = %q", code)
	}
	if code, _ := classifyFetchFailure(&net.DNSError{Err: "synthetic", Name: "tracker.example.invalid"}); code != "rule_source_dns_failed" {
		t.Fatalf("DNS code = %q", code)
	}
}

func TestCreateRunRejectsMissingExternalReadConfirmationBeforeDatabaseWork(t *testing.T) {
	service := &Service{}
	_, err := service.CreateRun(context.Background(), "MTEAM", CreateRunInput{
		ProviderID: "22222222-2222-4222-8222-222222222222", IdempotencyKey: "collection-test",
	}, workflow.Actor{Type: "user", ID: "11111111-1111-4111-8111-111111111111"})
	if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), "explicit confirmation") {
		t.Fatalf("error = %v", err)
	}
}

func TestNormalizeHTMLDropsActiveContentAndDetectsLogin(t *testing.T) {
	raw := []byte(`<html><style>.secret{}</style><body><h1>上传规则</h1><p>每种上传不得超过 100 MB/s</p><form><input type="password"></form><script>cookie()</script></body></html>`)
	text := string(normalizeSourceText(raw, "text/html"))
	if !strings.Contains(text, "上传规则") || !strings.Contains(text, "100 MB/s") || strings.Contains(text, "cookie()") || strings.Contains(text, "password") {
		t.Fatalf("normalized text = %q", text)
	}
	if !looksLikeLogin(raw) || looksLikeLogin([]byte("普通规则正文")) {
		t.Fatal("login-page classification mismatch")
	}
	if got := parseRetryAfter("120", time.Unix(0, 0)); got != 2*time.Minute {
		t.Fatalf("retry after = %s", got)
	}
}
