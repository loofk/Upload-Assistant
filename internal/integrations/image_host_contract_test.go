package integrations

import (
	"errors"
	"testing"
)

func TestValidateImageHostEndpointKeepsKeylessAdaptersOnOfficialServices(t *testing.T) {
	for _, test := range []struct {
		adapter  string
		endpoint string
		valid    bool
	}{
		{adapter: "imgbox", endpoint: "https://imgbox.com", valid: true},
		{adapter: "imgbox", endpoint: "https://imgbox.com/", valid: true},
		{adapter: "pixhost", endpoint: "https://api.pixhost.to/images", valid: true},
		{adapter: "pixhost", endpoint: "https://api.pixhost.cc/images", valid: true},
		{adapter: "pixhost", endpoint: "https://api.pixho.st/images", valid: true},
		{adapter: "imgbox", endpoint: "https://example.invalid", valid: false},
		{adapter: "pixhost", endpoint: "https://pixhost.to/images", valid: false},
		{adapter: "pixhost", endpoint: "https://api.pixhost.to/covers", valid: false},
		{adapter: "pixhost", endpoint: "http://api.pixhost.to/images", valid: false},
		{adapter: "pixhost", endpoint: "http://127.0.0.1:8080/images", valid: true},
		{adapter: "imgbb", endpoint: "https://private-proxy.invalid/upload", valid: true},
	} {
		err := ValidateImageHostEndpoint(test.adapter, test.endpoint)
		if test.valid && err != nil {
			t.Errorf("%s %s rejected: %v", test.adapter, test.endpoint, err)
		}
		if !test.valid && !errors.Is(err, ErrValidation) {
			t.Errorf("%s %s error = %v, want validation error", test.adapter, test.endpoint, err)
		}
	}
}
