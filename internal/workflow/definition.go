package workflow

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
)

type StepDefinition struct {
	Key      string `json:"key"`
	Required bool   `json:"required"`
	GateKind string `json:"gate_kind,omitempty"`
}

type Definition struct {
	Name    string           `json:"name"`
	Version int              `json:"version"`
	Steps   []StepDefinition `json:"steps"`
}

func RetorrentDefinition() Definition {
	return Definition{
		Name:    "retorrent",
		Version: 1,
		Steps: []StepDefinition{
			{Key: "source_parse", Required: true},
			{Key: "source_inspect", Required: true},
			{Key: "source_rules", Required: true, GateKind: "accept_rules"},
			{Key: "source_torrent", Required: true},
			{Key: "downloader_add", Required: true},
			{Key: "downloader_wait", Required: true},
			{Key: "content_resolve", Required: true},
			{Key: "metadata", Required: true},
			{Key: "media_info", Required: true},
			{Key: "screenshots", Required: true},
			{Key: "image_upload", Required: true},
			{Key: "target_package", Required: true},
			{Key: "target_duplicate_check", Required: true, GateKind: "duplicate_check"},
			{Key: "target_rules", Required: true, GateKind: "accept_rules"},
			{Key: "target_torrent", Required: true},
			{Key: "target_upload", Required: true, GateKind: "confirm_upload"},
			{Key: "target_torrent_download", Required: true},
			{Key: "target_inject", Required: true},
			{Key: "target_seed_verify", Required: true},
			{Key: "summary", Required: true},
		},
	}
}

func (d Definition) MarshalAndHash() ([]byte, string, error) {
	if d.Name == "" || d.Version <= 0 || len(d.Steps) == 0 {
		return nil, "", fmt.Errorf("invalid workflow definition")
	}
	seen := make(map[string]struct{}, len(d.Steps))
	for _, step := range d.Steps {
		if step.Key == "" {
			return nil, "", fmt.Errorf("workflow step key is required")
		}
		if _, exists := seen[step.Key]; exists {
			return nil, "", fmt.Errorf("duplicate workflow step key %q", step.Key)
		}
		seen[step.Key] = struct{}{}
	}
	body, err := json.Marshal(d)
	if err != nil {
		return nil, "", fmt.Errorf("marshal workflow definition: %w", err)
	}
	sum := sha256.Sum256(body)
	return body, hex.EncodeToString(sum[:]), nil
}
