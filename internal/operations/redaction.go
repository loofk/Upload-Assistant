package operations

import (
	"encoding/json"
	"net/url"
	"regexp"
	"strings"
)

const RedactedValue = "[REDACTED]"

var inlineSecret = regexp.MustCompile(`(?i)(passkey|authkey|api[_-]?key|access[_-]?token|token|password)=([^&\s]+)`)
var bearerSecret = regexp.MustCompile(`(?i)(authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]+`)
var cookieSecret = regexp.MustCompile(`(?i)(cookie\s*:)\s*[^\r\n]+`)

// Redact recursively removes credentials before an operational record or an
// LLM evidence snapshot crosses a trust boundary.
func Redact(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			if IsSecretField(key) {
				result[key] = RedactedValue
			} else {
				result[key] = Redact(item)
			}
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = Redact(item)
		}
		return result
	case json.RawMessage:
		var decoded any
		if json.Unmarshal(typed, &decoded) != nil {
			return map[string]any{"redacted": true}
		}
		return Redact(decoded)
	case string:
		return redactString(typed)
	default:
		return value
	}
}

func IsSecretField(key string) bool {
	normalized := strings.NewReplacer("_", "", "-", "", ".", "").Replace(strings.ToLower(strings.TrimSpace(key)))
	if strings.HasSuffix(normalized, "sha256") || strings.HasSuffix(normalized, "hash") {
		return false
	}
	for _, prefix := range []string{"prompttokens", "completiontokens", "inputtokens", "outputtokens", "totaltokens", "cachedtokens", "reasoningtokens", "maxcompletiontokens", "maxoutputtokens", "acceptedpredictiontokens", "rejectedpredictiontokens"} {
		if strings.HasPrefix(normalized, prefix) {
			return false
		}
	}
	for _, marker := range []string{"password", "passwd", "secret", "token", "cookie", "authorization", "apikey", "passkey", "authkey", "webhook", "announce"} {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return false
}

func redactString(value string) string {
	result := inlineSecret.ReplaceAllString(value, "$1="+RedactedValue)
	result = bearerSecret.ReplaceAllString(result, "$1 "+RedactedValue)
	result = cookieSecret.ReplaceAllString(result, "$1 "+RedactedValue)
	parsed, err := url.Parse(result)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return result
	}
	parsed.User = nil
	query := parsed.Query()
	for key := range query {
		if IsSecretField(key) {
			query.Set(key, RedactedValue)
		}
	}
	parsed.RawQuery = query.Encode()
	segments := strings.Split(parsed.Path, "/")
	for index, segment := range segments {
		if strings.EqualFold(segment, "announce") && index+1 < len(segments) {
			segments[index+1] = RedactedValue
			parsed.Path = strings.Join(segments, "/")
			break
		}
	}
	return parsed.String()
}
