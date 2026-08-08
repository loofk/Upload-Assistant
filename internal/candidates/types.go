package candidates

import (
	"encoding/json"
	"errors"
	"time"
)

var (
	ErrNotFound       = errors.New("candidate item not found")
	ErrNotSubmittable = errors.New("candidate item is no longer submittable")
)

type Status string

const (
	StatusCandidate Status = "candidate"
	StatusBlocked   Status = "blocked"
	StatusSubmitted Status = "submitted"
	StatusExpired   Status = "expired"
)

type Item struct {
	ID                 string          `json:"id"`
	ScheduleID         string          `json:"schedule_id,omitempty"`
	DiscoveryJobID     string          `json:"discovery_job_id,omitempty"`
	SubmittedJobID     string          `json:"submitted_job_id,omitempty"`
	SourceSite         string          `json:"source_site"`
	TargetSite         string          `json:"target_site"`
	SourceTorrentID    string          `json:"source_torrent_id"`
	RecommendationDate time.Time       `json:"recommendation_date"`
	Rank               *int            `json:"rank,omitempty"`
	Score              float64         `json:"score"`
	Payload            json.RawMessage `json:"payload"`
	Status             Status          `json:"status"`
	DiscoveredAt       time.Time       `json:"discovered_at"`
	ExpiresAt          time.Time       `json:"expires_at"`
	UpdatedAt          time.Time       `json:"updated_at"`
	SubmittedAt        *time.Time      `json:"submitted_at,omitempty"`
}

type UpsertInput struct {
	ScheduleID         string
	DiscoveryJobID     string
	SourceSite         string
	TargetSite         string
	SourceTorrentID    string
	RecommendationDate time.Time
	Rank               *int
	Score              float64
	Payload            json.RawMessage
	Status             Status
	ExpiresAt          time.Time
}

type ListFilter struct {
	SourceSite         string
	TargetSite         string
	RecommendationDate *time.Time
	Status             Status
	Limit              int
}
