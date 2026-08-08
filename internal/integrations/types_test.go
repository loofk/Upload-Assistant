package integrations

import (
	"encoding/json"
	"errors"
	"testing"
)

func TestValidateEndpointConfigSeparatesSecrets(t *testing.T) {
	body, err := validateEndpointConfig(EndpointConfig{
		Endpoint: "http://host.docker.internal:8080/", Options: map[string]any{"category": "retorrent"},
	})
	if err != nil {
		t.Fatalf("validateEndpointConfig() error = %v", err)
	}
	var result EndpointConfig
	if err := json.Unmarshal(body, &result); err != nil {
		t.Fatal(err)
	}
	if result.Endpoint != "http://host.docker.internal:8080" || result.TimeoutSeconds != 30 {
		t.Fatalf("normalized endpoint config = %#v", result)
	}

	_, err = validateEndpointConfig(EndpointConfig{
		Endpoint: "http://localhost:8080", Options: map[string]any{"api_token": "must-not-be-here"},
	})
	if !errors.Is(err, ErrValidation) {
		t.Fatalf("secret option error = %v, want ErrValidation", err)
	}
	if _, err := validateEndpointConfig(EndpointConfig{Endpoint: "file:///tmp/socket"}); !errors.Is(err, ErrValidation) {
		t.Fatalf("file endpoint error = %v, want ErrValidation", err)
	}
}

func TestValidateMappingsRequiresNormalizedAbsolutePaths(t *testing.T) {
	if err := validateMappings([]PathMapping{{RemotePath: "/downloads", LocalPath: "/downloads"}}); err != nil {
		t.Fatalf("validateMappings() error = %v", err)
	}
	for _, mappings := range [][]PathMapping{
		{{RemotePath: "downloads", LocalPath: "/downloads"}},
		{{RemotePath: "/remote/../downloads", LocalPath: "/downloads"}},
		{{RemotePath: "/remote", LocalPath: "/one"}, {RemotePath: "/remote", LocalPath: "/two"}},
	} {
		if err := validateMappings(mappings); !errors.Is(err, ErrValidation) {
			t.Fatalf("validateMappings(%#v) error = %v", mappings, err)
		}
	}
}

func TestNormalizeScreenshotConfig(t *testing.T) {
	config, body, err := normalizeScreenshotConfig(map[string]any{"count": 8, "format": "WEBP", "width": 1920})
	if err != nil {
		t.Fatal(err)
	}
	if config.Count != 8 || config.Format != "webp" || config.Quality != 90 || config.StartPercent != 0.1 || !json.Valid(body) {
		t.Fatalf("normalized screenshot config = %#v body=%s", config, body)
	}
	for _, input := range []map[string]any{
		{"count": 30}, {"format": "bmp"}, {"width": 100}, {"unknown": true},
	} {
		if _, _, err := normalizeScreenshotConfig(input); !errors.Is(err, ErrValidation) {
			t.Fatalf("normalizeScreenshotConfig(%#v) error = %v", input, err)
		}
	}
}
