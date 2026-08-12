package rulecollector

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"mime"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	ruleSourceConnectTimeout      = 20 * time.Second
	ruleSourceTLSHandshakeTimeout = 20 * time.Second
	ruleSourceUserAgent           = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

type fetchResult struct {
	HTTPStatus     int
	ResponseSHA256 string
	RetryAfter     time.Duration
	ErrorCode      string
}

func safeHTTPClient(timeout time.Duration) *http.Client {
	dialer := &net.Dialer{Timeout: ruleSourceConnectTimeout, KeepAlive: 30 * time.Second}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	handshakeTimeout := ruleSourceTLSHandshakeTimeout
	if timeout > 0 && timeout < handshakeTimeout {
		handshakeTimeout = timeout
	}
	// Authenticated rule-page traffic must not inherit an ambient proxy: doing
	// so could disclose the exact Cookie outside the explicitly confirmed host.
	transport.Proxy = nil
	transport.TLSHandshakeTimeout = handshakeTimeout
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		return dialResolvedPublicHost(ctx, network, address, net.DefaultResolver.LookupIPAddr,
			func(ctx context.Context, network, address, _ string) (net.Conn, error) {
				connection, err := dialer.DialContext(ctx, network, address)
				if err != nil {
					return nil, &ruleSourceNetworkError{Phase: "connect", Err: err}
				}
				return connection, nil
			})
	}
	transport.DialTLSContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		return dialResolvedPublicHost(ctx, network, address, net.DefaultResolver.LookupIPAddr,
			func(ctx context.Context, network, address, serverName string) (net.Conn, error) {
				connection, err := dialer.DialContext(ctx, network, address)
				if err != nil {
					return nil, &ruleSourceNetworkError{Phase: "connect", Err: err}
				}
				configuration := &tls.Config{ServerName: serverName, MinVersion: tls.VersionTLS12}
				if transport.TLSClientConfig != nil {
					configuration = transport.TLSClientConfig.Clone()
					if configuration.ServerName == "" {
						configuration.ServerName = serverName
					}
				}
				tlsConnection := tls.Client(connection, configuration)
				handshakeContext, cancel := context.WithTimeout(ctx, handshakeTimeout)
				defer cancel()
				if err := tlsConnection.HandshakeContext(handshakeContext); err != nil {
					_ = connection.Close()
					return nil, &ruleSourceNetworkError{Phase: "tls", Err: err}
				}
				return tlsConnection, nil
			})
	}
	return &http.Client{
		Transport: transport, Timeout: timeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}
}

type lookupIPAddrFunc func(context.Context, string) ([]net.IPAddr, error)
type dialResolvedAddressFunc func(context.Context, string, string, string) (net.Conn, error)

type ruleSourceNetworkError struct {
	Phase string
	Err   error
}

func (e *ruleSourceNetworkError) Error() string {
	return "rule source " + e.Phase + " failed: " + e.Err.Error()
}
func (e *ruleSourceNetworkError) Unwrap() error { return e.Err }

func dialResolvedPublicHost(ctx context.Context, network, address string, lookup lookupIPAddrFunc, dial dialResolvedAddressFunc) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	addresses, err := lookup(ctx, host)
	if err != nil {
		return nil, &ruleSourceNetworkError{Phase: "dns", Err: err}
	}
	if len(addresses) == 0 {
		return nil, fmt.Errorf("rule source host did not resolve to an address")
	}
	// Reject the complete answer if it contains any non-public destination. This
	// prevents a mixed DNS answer from being used to bypass the private-network
	// boundary while still allowing fallback between validated public addresses.
	for _, candidate := range addresses {
		if !publicIP(candidate.IP) {
			return nil, fmt.Errorf("rule source host resolves to a forbidden address")
		}
	}
	var lastErr error
	for _, candidate := range addresses {
		connection, err := dial(ctx, network, net.JoinHostPort(candidate.IP.String(), port), host)
		if err == nil {
			return connection, nil
		}
		lastErr = err
		if ctx.Err() != nil {
			break
		}
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("no usable public address")
	}
	return nil, fmt.Errorf("rule source connection failed: %w", lastErr)
}

func publicIP(ip net.IP) bool {
	return ip != nil && !ip.IsLoopback() && !ip.IsPrivate() && !ip.IsUnspecified() && !ip.IsMulticast() && !ip.IsLinkLocalUnicast() && !ip.IsLinkLocalMulticast()
}

func (s *Service) fetch(ctx context.Context, runID string, document CollectionDocument, cookie string) (fetchResult, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, document.URL, nil)
	if err != nil {
		return fetchResult{ErrorCode: "rule_source_url_invalid"}, err
	}
	request.Header.Set("Accept", "text/html, text/markdown, text/plain;q=0.9")
	if document.AuthMode == SourceAuthSiteCookie {
		if strings.TrimSpace(cookie) == "" {
			return fetchResult{ErrorCode: "site_cookie_required"}, fmt.Errorf("读取该规则页需要站点 Cookie")
		}
		normalizedCookie, err := normalizeCookieHeader(cookie)
		if err != nil {
			return fetchResult{ErrorCode: "site_cookie_invalid"}, fmt.Errorf("站点 Cookie 格式无效；请粘贴浏览器请求中的完整 Cookie")
		}
		request.Header.Set("Cookie", normalizedCookie)
	}
	request.Header.Set("User-Agent", ruleSourceUserAgent)
	request.Header.Set("Accept-Language", "zh-CN,zh-TW;q=0.9,en;q=0.7")
	response, err := s.httpClient.Do(request)
	if err != nil {
		code, detail := classifyFetchFailure(err)
		return fetchResult{ErrorCode: code}, errors.New(detail)
	}
	defer response.Body.Close()
	limited := io.LimitReader(response.Body, MaxRawResponseBytes+1)
	raw, readErr := io.ReadAll(limited)
	digest := sha256.Sum256(raw)
	result := fetchResult{HTTPStatus: response.StatusCode, ResponseSHA256: hex.EncodeToString(digest[:]), RetryAfter: parseRetryAfter(response.Header.Get("Retry-After"), s.now())}
	if readErr != nil {
		result.ErrorCode = "rule_source_read_failed"
		return result, fmt.Errorf("读取规则页响应失败")
	}
	if len(raw) > MaxRawResponseBytes {
		result.ErrorCode = "rule_source_response_too_large"
		return result, fmt.Errorf("规则页响应超过 %d MiB 上限", MaxRawResponseBytes>>20)
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		result.ErrorCode = "rule_source_authentication_failed"
		if document.AuthMode == SourceAuthSiteCookie {
			return result, fmt.Errorf("站点拒绝了现有 Cookie（HTTP %d）", response.StatusCode)
		}
		return result, fmt.Errorf("公开规则页拒绝了无凭据访问（HTTP %d）；请确认该页面是否需要站点 Cookie", response.StatusCode)
	}
	if response.StatusCode == http.StatusTooManyRequests {
		result.ErrorCode = "rule_source_rate_limited"
		return result, fmt.Errorf("站点返回 HTTP 429，已记录冷却时间")
	}
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		result.ErrorCode = "rule_source_redirect_rejected"
		return result, fmt.Errorf("规则页发生重定向；请检查精确页面地址和访问方式")
	}
	if response.StatusCode != http.StatusOK {
		result.ErrorCode = "rule_source_http_error"
		return result, fmt.Errorf("规则页返回 HTTP %d", response.StatusCode)
	}
	contentType, _, _ := mime.ParseMediaType(response.Header.Get("Content-Type"))
	contentType = strings.ToLower(strings.TrimSpace(contentType))
	if contentType == "" {
		contentType = strings.ToLower(http.DetectContentType(raw))
		if semi := strings.IndexByte(contentType, ';'); semi >= 0 {
			contentType = contentType[:semi]
		}
	}
	if contentType != "text/html" && contentType != "text/plain" && contentType != "text/markdown" {
		result.ErrorCode = "rule_source_content_type_unsupported"
		return result, fmt.Errorf("规则页内容类型 %s 不受支持", contentType)
	}
	if looksLikeLogin(raw) {
		result.ErrorCode = "rule_source_authentication_failed"
		if document.AuthMode == SourceAuthSiteCookie {
			return result, fmt.Errorf("规则页返回了登录或访问验证页面；请更新 Cookie")
		}
		return result, fmt.Errorf("公开规则页返回了登录或访问验证页面；请将该来源改为站点 Cookie，或检查页面地址")
	}
	text := normalizeSourceText(raw, contentType)
	if len(text) == 0 {
		result.ErrorCode = "rule_source_text_empty"
		return result, fmt.Errorf("规则页没有可分析的正文")
	}
	if len(text) > MaxNormalizedTextBytes {
		result.ErrorCode = "rule_source_text_too_large"
		return result, fmt.Errorf("规范化规则正文超过 %d MiB 上限", MaxNormalizedTextBytes>>20)
	}
	textDigest := sha256.Sum256(text)
	textSHA := hex.EncodeToString(textDigest[:])
	relative := filepath.Join("site-rules", "collections", runID, document.SourceID+".md")
	directory := filepath.Join(s.dataDir, filepath.Dir(relative))
	if err := os.MkdirAll(directory, 0o750); err != nil {
		result.ErrorCode = "rule_source_persist_failed"
		return result, fmt.Errorf("保存规则正文目录失败")
	}
	temporary, err := os.CreateTemp(directory, ".source-*.tmp")
	if err != nil {
		result.ErrorCode = "rule_source_persist_failed"
		return result, fmt.Errorf("创建规则正文临时文件失败")
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	_ = temporary.Chmod(0o600)
	if _, err = temporary.Write(text); err == nil {
		err = temporary.Sync()
	}
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(temporaryName, filepath.Join(s.dataDir, relative))
	}
	if err != nil {
		result.ErrorCode = "rule_source_persist_failed"
		return result, fmt.Errorf("持久化规则正文失败")
	}
	capturedAt := s.now().UTC()
	_, err = s.pool.Exec(ctx, `UPDATE site_rule_collection_documents SET status='ready',http_status=$2,content_type=$3,size_bytes=$4,
		text_sha256=$5,storage_path=$6,captured_at=$7,error_code=NULL,error_detail=NULL,updated_at=now() WHERE id=$1`,
		document.ID, response.StatusCode, contentType, len(text), textSHA, filepath.ToSlash(relative), capturedAt)
	if err != nil {
		result.ErrorCode = "rule_source_persist_failed"
		return result, fmt.Errorf("记录规则正文证据失败")
	}
	return result, nil
}

func normalizeCookieHeader(value string) (string, error) {
	value = strings.TrimSpace(value)
	if strings.ContainsAny(value, "\r\n") {
		return "", fmt.Errorf("cookie contains a line break")
	}
	if len(value) >= len("cookie:") && strings.EqualFold(value[:len("cookie:")], "cookie:") {
		value = strings.TrimSpace(value[len("cookie:"):])
	}
	value = strings.TrimRight(value, "; \t")
	if value == "" {
		return "", fmt.Errorf("cookie is empty")
	}
	if strings.HasPrefix(value, "{") {
		var values map[string]string
		if err := json.Unmarshal([]byte(value), &values); err != nil || len(values) == 0 {
			return "", fmt.Errorf("cookie JSON is invalid")
		}
		names := make([]string, 0, len(values))
		for name := range values {
			names = append(names, name)
		}
		sort.Strings(names)
		parts := make([]string, 0, len(names))
		for _, name := range names {
			parsed, err := http.ParseCookie(name + "=" + values[name])
			if err != nil || len(parsed) != 1 || parsed[0].Name != name || parsed[0].Value != values[name] {
				return "", fmt.Errorf("cookie JSON contains an invalid pair")
			}
			parts = append(parts, parsed[0].String())
		}
		return strings.Join(parts, "; "), nil
	}
	cookies, err := http.ParseCookie(value)
	if err != nil || len(cookies) == 0 {
		return "", fmt.Errorf("cookie header is invalid")
	}
	parts := make([]string, 0, len(cookies))
	for _, cookie := range cookies {
		serialized := (&http.Cookie{Name: cookie.Name, Value: cookie.Value, Quoted: cookie.Quoted}).String()
		if serialized == "" {
			return "", fmt.Errorf("cookie header contains an invalid pair")
		}
		parts = append(parts, serialized)
	}
	return strings.Join(parts, "; "), nil
}

func classifyFetchFailure(err error) (string, string) {
	message := strings.ToLower(err.Error())
	var phaseError *ruleSourceNetworkError
	var dnsError *net.DNSError
	switch {
	case errors.As(err, &phaseError) && phaseError.Phase == "dns":
		return "rule_source_dns_failed", "规则页域名解析失败"
	case errors.As(err, &phaseError) && phaseError.Phase == "connect" && networkTimeout(err):
		return "rule_source_connection_timeout", fmt.Sprintf("规则页 TCP 连接在 %d 秒内未建立", int(ruleSourceConnectTimeout/time.Second))
	case errors.As(err, &phaseError) && phaseError.Phase == "connect":
		return "rule_source_connection_failed", "无法连接规则页服务器"
	case errors.As(err, &phaseError) && phaseError.Phase == "tls" && networkTimeout(err):
		return "rule_source_tls_timeout", fmt.Sprintf("规则页 TLS 握手在 %d 秒内未完成", int(ruleSourceTLSHandshakeTimeout/time.Second))
	case errors.As(err, &phaseError) && phaseError.Phase == "tls":
		return "rule_source_tls_failed", "规则页 TLS 握手或证书验证失败"
	case errors.As(err, &dnsError):
		return "rule_source_dns_failed", "规则页域名解析失败"
	case strings.Contains(message, "tls handshake") && networkTimeout(err):
		return "rule_source_tls_timeout", "规则页 TLS 握手超时"
	case strings.Contains(message, "tls handshake") || strings.Contains(message, "x509"):
		return "rule_source_tls_failed", "规则页 TLS 握手或证书验证失败"
	case errors.Is(err, context.DeadlineExceeded) || strings.Contains(message, "client.timeout"):
		return "rule_source_timeout", "读取规则页超时"
	case strings.Contains(message, "connection refused") || strings.Contains(message, "network is unreachable") || strings.Contains(message, "no route to host"):
		return "rule_source_connection_failed", "无法连接规则页服务器"
	default:
		return "rule_source_fetch_failed", "读取规则页失败"
	}
}

func networkTimeout(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var timeout interface{ Timeout() bool }
	return errors.As(err, &timeout) && timeout.Timeout()
}

var (
	scriptBlockPattern = regexp.MustCompile(`(?is)<(?:script|style|noscript|svg|form)[^>]*>.*?</(?:script|style|noscript|svg|form)>`)
	commentPattern     = regexp.MustCompile(`(?is)<!--.*?-->`)
	lineBreakPattern   = regexp.MustCompile(`(?is)</?(?:h[1-6]|p|div|section|article|header|footer|nav|aside|ul|ol|li|table|thead|tbody|tfoot|tr|blockquote|pre|br)[^>]*>`)
	cellBreakPattern   = regexp.MustCompile(`(?is)</?(?:td|th)[^>]*>`)
	tagPattern         = regexp.MustCompile(`(?is)<[^>]+>`)
	spacePattern       = regexp.MustCompile(`[\t\x0b\f\r ]+`)
	blankLinesPattern  = regexp.MustCompile(`\n{3,}`)
)

func normalizeSourceText(raw []byte, contentType string) []byte {
	value := strings.ReplaceAll(string(raw), "\r\n", "\n")
	if contentType == "text/html" {
		value = scriptBlockPattern.ReplaceAllString(value, "\n")
		value = commentPattern.ReplaceAllString(value, "\n")
		value = cellBreakPattern.ReplaceAllString(value, "\t")
		value = lineBreakPattern.ReplaceAllString(value, "\n")
		value = tagPattern.ReplaceAllString(value, "")
		value = html.UnescapeString(value)
	}
	lines := strings.Split(value, "\n")
	result := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(spacePattern.ReplaceAllString(line, " "))
		if line != "" {
			result = append(result, line)
		} else if len(result) > 0 && result[len(result)-1] != "" {
			result = append(result, "")
		}
	}
	return []byte(strings.TrimSpace(blankLinesPattern.ReplaceAllString(strings.Join(result, "\n"), "\n\n")))
}

func looksLikeLogin(raw []byte) bool {
	value := strings.ToLower(string(raw))
	markers := []string{
		`type="password"`, `type='password'`, "name=\"password\"", "name='password'",
		"请先登录", "登入後才能", "登录后才能", "cloudflare ray id", "checking your browser",
	}
	for _, marker := range markers {
		if strings.Contains(value, marker) {
			return true
		}
	}
	return false
}

func parseRetryAfter(value string, now time.Time) time.Duration {
	value = strings.TrimSpace(value)
	if seconds, err := strconv.Atoi(value); err == nil && seconds > 0 {
		return time.Duration(seconds) * time.Second
	}
	if timestamp, err := http.ParseTime(value); err == nil && timestamp.After(now) {
		return timestamp.Sub(now)
	}
	return 0
}
