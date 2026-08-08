package legacy

import (
	"os"
	"strings"
	"testing"
)

func TestParseConfigLiteralParsesRepositoryTemplate(t *testing.T) {
	body, err := os.ReadFile("../../data/templates/config.py")
	if err != nil {
		t.Fatal(err)
	}
	config, err := ParseConfigLiteral(body)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := config["TRACKERS"].(map[string]any); !ok {
		t.Fatalf("template TRACKERS section = %#v", config["TRACKERS"])
	}
}

func TestParseConfigLiteralParsesRepositoryExample(t *testing.T) {
	body, err := os.ReadFile("../../data/example-config.py")
	if err != nil {
		t.Fatal(err)
	}
	config, err := ParseConfigLiteral(body)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := config["TORRENT_CLIENTS"].(map[string]any); !ok {
		t.Fatal("example TORRENT_CLIENTS section is missing")
	}
}

func TestParseConfigLiteralParsesLegacySubset(t *testing.T) {
	config, err := ParseConfigLiteral([]byte(`# comment
config = {
  "DEFAULT": {"screens": "4", "tone_map": True, "optional": None},
  'TRACKERS': {'U2': {'passkey': 'secret\x2dvalue'}},
  "TORRENT_CLIENTS": {"box": {"local_path": ["/downloads"], "port": 8080}},
}
`))
	if err != nil {
		t.Fatal(err)
	}
	defaults := config["DEFAULT"].(map[string]any)
	trackers := config["TRACKERS"].(map[string]any)
	if defaults["screens"] != "4" || defaults["tone_map"] != true || defaults["optional"] != nil || trackers["U2"].(map[string]any)["passkey"] != "secret-value" {
		t.Fatalf("config = %#v", config)
	}
}

func TestParseConfigLiteralRejectsExecutablePythonAndDuplicates(t *testing.T) {
	for _, body := range []string{
		`import os
config = {}`,
		`config = {"x": os.getenv("SECRET")}`,
		`config = {"x": 1, "x": 2}`,
		`config = {}; print("secret")`,
	} {
		if _, err := ParseConfigLiteral([]byte(body)); err == nil {
			t.Fatalf("ParseConfigLiteral accepted %q", body)
		} else if strings.Contains(err.Error(), "SECRET") || strings.Contains(err.Error(), "secret") {
			t.Fatalf("parser error exposed source content: %v", err)
		}
	}
}

func TestParseConfigLiteralRejectsOversizedInput(t *testing.T) {
	if _, err := ParseConfigLiteral([]byte("config = {\"x\": \"" + strings.Repeat("x", maxConfigBytes) + "\"}")); err == nil {
		t.Fatal("oversized config was accepted")
	}
}
