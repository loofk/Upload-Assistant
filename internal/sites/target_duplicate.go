package sites

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type TargetDuplicateQuery struct {
	IMDbID string `json:"imdb_id"`
}

type TargetDuplicateCandidate struct {
	ID        string `json:"id,omitempty"`
	Name      string `json:"name"`
	SizeBytes int64  `json:"size_bytes,omitempty"`
	Category  string `json:"category,omitempty"`
	Standard  string `json:"standard,omitempty"`
	IMDbID    string `json:"imdb_id,omitempty"`
	CreatedAt string `json:"created_at,omitempty"`
}

type TargetDuplicateEvidence struct {
	SiteCode            string                     `json:"site_code"`
	Adapter             string                     `json:"adapter"`
	ConfigurationSHA256 string                     `json:"configuration_sha256"`
	Query               TargetDuplicateQuery       `json:"query"`
	Duplicate           bool                       `json:"duplicate"`
	ResultCount         int                        `json:"result_count"`
	Candidates          []TargetDuplicateCandidate `json:"candidates"`
	CandidatesTruncated bool                       `json:"candidates_truncated"`
	CheckedAt           time.Time                  `json:"checked_at"`
}

type TargetDuplicateAdapter interface {
	SiteCode() string
	DuplicateCheck(context.Context, TargetDuplicateQuery, workflow.Actor) (TargetDuplicateEvidence, error)
}

type TargetDuplicateRegistry struct {
	adapters map[string]TargetDuplicateAdapter
}

func NewTargetDuplicateRegistry(adapters ...TargetDuplicateAdapter) (*TargetDuplicateRegistry, error) {
	registry := &TargetDuplicateRegistry{adapters: map[string]TargetDuplicateAdapter{}}
	for _, adapter := range adapters {
		if adapter == nil {
			return nil, fmt.Errorf("target duplicate adapter is nil")
		}
		code := strings.ToUpper(strings.TrimSpace(adapter.SiteCode()))
		if code == "" {
			return nil, fmt.Errorf("target duplicate adapter code is required")
		}
		if _, exists := registry.adapters[code]; exists {
			return nil, fmt.Errorf("target duplicate adapter %s is already registered", code)
		}
		registry.adapters[code] = adapter
	}
	return registry, nil
}

func (registry *TargetDuplicateRegistry) DuplicateCheck(ctx context.Context, siteCode string, query TargetDuplicateQuery, actor workflow.Actor) (TargetDuplicateEvidence, error) {
	code := strings.ToUpper(strings.TrimSpace(siteCode))
	adapter, exists := registry.adapters[code]
	if !exists {
		return TargetDuplicateEvidence{}, NewAdapterError(
			"target_duplicate_adapter_unavailable",
			fmt.Sprintf("target duplicate adapter for %s is not implemented", code), false, nil,
		)
	}
	return adapter.DuplicateCheck(ctx, query, actor)
}
