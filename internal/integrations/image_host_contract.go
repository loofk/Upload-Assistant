package integrations

import (
	"fmt"
	"net/url"
	"strings"
)

// ValidateImageHostEndpoint keeps anonymous upload adapters on their official
// service domains. Loopback HTTP(S) endpoints remain available only for local
// isolated tests; credentialed adapters retain their existing explicit custom
// endpoint support.
func ValidateImageHostEndpoint(adapter, value string) error {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed.Host == "" {
		return fmt.Errorf("%w: image host endpoint is invalid", ErrValidation)
	}
	host := strings.ToLower(parsed.Hostname())
	if host == "localhost" || host == "127.0.0.1" || host == "::1" {
		return nil
	}
	if parsed.Scheme != "https" || (parsed.Port() != "" && parsed.Port() != "443") {
		return fmt.Errorf("%w: image host endpoint must use the official HTTPS service port", ErrValidation)
	}
	switch strings.ToLower(strings.TrimSpace(adapter)) {
	case "imgbox":
		if host != "imgbox.com" || (parsed.Path != "" && parsed.Path != "/") {
			return fmt.Errorf("%w: Imgbox endpoint must be https://imgbox.com", ErrValidation)
		}
	case "pixhost":
		if !slicesContainsString([]string{"api.pixhost.to", "api.pixhost.cc", "api.pixho.st"}, host) || parsed.Path != "/images" {
			return fmt.Errorf("%w: Pixhost endpoint must use an official API v2 /images URL", ErrValidation)
		}
	}
	return nil
}

func slicesContainsString(values []string, value string) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}
