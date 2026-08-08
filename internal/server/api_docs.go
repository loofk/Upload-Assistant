package server

import (
	_ "embed"
	"net/http"
)

//go:embed openapi.json
var openAPIDocument []byte

type toolDefinition struct {
	Name           string         `json:"name"`
	Description    string         `json:"description"`
	Method         string         `json:"method"`
	Path           string         `json:"path"`
	RequiredScopes []string       `json:"required_scopes"`
	SafetyLevel    string         `json:"safety_level"`
	InputSchema    map[string]any `json:"input_schema"`
}

func registerDocumentationRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /openapi.json", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.oai.openapi+json;version=3.1")
		w.Header().Set("Cache-Control", "public, max-age=60")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(openAPIDocument)
	})
	mux.HandleFunc("GET /api/v2/tools", func(w http.ResponseWriter, r *http.Request) {
		if _, ok := requireScope(w, r, "config:read"); !ok {
			return
		}
		tools := toolDefinitions()
		writeJSON(w, http.StatusOK, map[string]any{
			"ok": true, "status": "ready", "api_version": "v2",
			"count": len(tools), "tools": tools,
			"blockers": []any{}, "next_actions": []any{},
		})
	})
}

func toolDefinitions() []toolDefinition {
	object := func(properties map[string]any, required ...string) map[string]any {
		result := map[string]any{
			"type": "object", "properties": properties, "additionalProperties": false,
		}
		if len(required) > 0 {
			result["required"] = required
		}
		return result
	}
	stringProperty := func(description string) map[string]any {
		return map[string]any{"type": "string", "description": description}
	}
	jobID := object(map[string]any{"job_id": stringProperty("UUID of the durable job.")}, "job_id")
	return []toolDefinition{
		{Name: "list_jobs", Description: "List recent durable jobs with optional status filtering and an opaque stable cursor.", Method: "GET", Path: "/api/v2/jobs", RequiredScopes: []string{"jobs:read"}, SafetyLevel: "read_only", InputSchema: object(map[string]any{"status": map[string]any{"type": "string", "enum": []string{"draft", "queued", "running", "paused", "blocked", "failed", "complete", "cancelled"}}, "kind": map[string]any{"type": "string", "const": "retorrent"}, "limit": map[string]any{"type": "integer", "minimum": 1, "maximum": 100}, "cursor": stringProperty("Opaque cursor returned by the previous page.")})},
		{
			Name: "create_retorrent_job", Description: "Create an auditable retorrent workflow and return job_id immediately. Every source, rule, duplicate, material, upload, target-torrent, qBittorrent injection, seeding, and summary boundary persists separate evidence; live upload requires explicit accept_rules and confirm_upload gates.",
			Method: "POST", Path: "/api/v2/jobs", RequiredScopes: []string{"jobs:write"}, SafetyLevel: "controlled_write",
			InputSchema: object(map[string]any{
				"idempotency_key": stringProperty("Required Idempotency-Key HTTP header value."),
				"kind":            map[string]any{"type": "string", "const": "retorrent"},
				"execution_mode":  map[string]any{"type": "string", "enum": []string{"auto", "step"}},
				"stop_after_step": stringProperty("Optional workflow step boundary."),
				"input":           map[string]any{"$ref": "#/components/schemas/RetorrentInput"},
			}, "idempotency_key", "input"),
		},
		{Name: "get_job_status", Description: "Read status, blockers, next_actions, and resume_state.", Method: "GET", Path: "/api/v2/jobs/{job_id}", RequiredScopes: []string{"jobs:read"}, SafetyLevel: "read_only", InputSchema: jobID},
		{Name: "get_job_summary", Description: "Read status/blockers/resume data and, after completion, the stable retorrent summary with source/target hashes, rule fingerprints, duplicate gates, qBittorrent seeding checks, and summary_file evidence.", Method: "GET", Path: "/api/v2/jobs/{job_id}/summary", RequiredScopes: []string{"jobs:read"}, SafetyLevel: "read_only", InputSchema: jobID},
		{Name: "get_job_events", Description: "Read the append-only hash-chained job event audit stream.", Method: "GET", Path: "/api/v2/jobs/{job_id}/events", RequiredScopes: []string{"jobs:read"}, SafetyLevel: "read_only", InputSchema: object(map[string]any{"job_id": stringProperty("Job UUID."), "after": map[string]any{"type": "integer", "minimum": 0}, "limit": map[string]any{"type": "integer", "minimum": 1, "maximum": 500}}, "job_id")},
		{Name: "download_artifact_evidence", Description: "Download a non-secret evidence artifact after the service re-verifies its immutable size and SHA-256. Raw torrent artifacts are always denied.", Method: "GET", Path: "/api/v2/jobs/{job_id}/artifacts/{artifact_id}/content", RequiredScopes: []string{"jobs:read", "audit:read"}, SafetyLevel: "read_only", InputSchema: object(map[string]any{"job_id": stringProperty("Job UUID."), "artifact_id": stringProperty("Artifact UUID from the same job.")}, "job_id", "artifact_id")},
		{Name: "resume_job", Description: "Resume a paused, blocked, or failed job with explicit recovery values.", Method: "POST", Path: "/api/v2/jobs/{job_id}/resume", RequiredScopes: []string{"jobs:write"}, SafetyLevel: "controlled_write", InputSchema: object(map[string]any{"job_id": stringProperty("Job UUID."), "resume_state": map[string]any{"type": "object", "additionalProperties": true}}, "job_id", "resume_state")},
		{Name: "pause_job", Description: "Pause a runnable job at its durable current step.", Method: "POST", Path: "/api/v2/jobs/{job_id}/pause", RequiredScopes: []string{"jobs:write"}, SafetyLevel: "controlled_write", InputSchema: jobID},
		{Name: "cancel_job", Description: "Cancel a job. This does not delete artifacts or downloaded data.", Method: "POST", Path: "/api/v2/jobs/{job_id}/cancel", RequiredScopes: []string{"jobs:write"}, SafetyLevel: "controlled_write", InputSchema: jobID},
		{Name: "list_sites", Description: "List configured site adapters and active rule fingerprints.", Method: "GET", Path: "/api/v2/sites", RequiredScopes: []string{"config:read"}, SafetyLevel: "read_only", InputSchema: object(map[string]any{})},
		{Name: "get_active_site_rules", Description: "Read the active approved rule revision for a site.", Method: "GET", Path: "/api/v2/sites/{site_code}/rules/active", RequiredScopes: []string{"config:read"}, SafetyLevel: "read_only", InputSchema: object(map[string]any{"site_code": stringProperty("Uppercase tracker code.")}, "site_code")},
		{Name: "import_site_rules", Description: "Import an immutable Markdown rule revision as a draft.", Method: "POST", Path: "/api/v2/site-rules/import", RequiredScopes: []string{"config:manage"}, SafetyLevel: "configuration_write", InputSchema: object(map[string]any{"markdown": stringProperty("Complete Markdown document with YAML front matter and original rule text.")}, "markdown")},
		{Name: "approve_site_rules", Description: "Approve a complete draft using its exact fingerprint.", Method: "POST", Path: "/api/v2/site-rules/{revision_id}/approve", RequiredScopes: []string{"config:manage"}, SafetyLevel: "privileged_write", InputSchema: object(map[string]any{"revision_id": stringProperty("Rule revision UUID."), "fingerprint": stringProperty("Exact SHA-256 policy fingerprint."), "comment": stringProperty("Reviewer audit comment.")}, "revision_id", "fingerprint")},
		{Name: "activate_site_rules", Description: "Activate an approved rule revision for workflow enforcement.", Method: "POST", Path: "/api/v2/site-rules/{revision_id}/activate", RequiredScopes: []string{"config:manage"}, SafetyLevel: "privileged_write", InputSchema: object(map[string]any{"revision_id": stringProperty("Approved rule revision UUID.")}, "revision_id")},
		{Name: "configure_downloader", Description: "Add or update a remote downloader, encrypted credentials, and path mappings.", Method: "PUT", Path: "/api/v2/downloaders/{name}", RequiredScopes: []string{"downloader:manage"}, SafetyLevel: "configuration_write", InputSchema: object(map[string]any{"name": stringProperty("Stable downloader name."), "adapter": map[string]any{"type": "string", "enum": []string{"qbittorrent", "rtorrent", "deluge", "transmission"}}, "enabled": map[string]any{"type": "boolean"}, "config": map[string]any{"$ref": "#/components/schemas/EndpointConfig"}, "credentials": map[string]any{"type": "object", "additionalProperties": map[string]any{"type": "string"}}, "path_mappings": map[string]any{"type": "array", "items": map[string]any{"$ref": "#/components/schemas/PathMapping"}}}, "name", "adapter", "config")},
		{Name: "configure_image_host", Description: "Add or update an independently prioritized image host with encrypted credentials.", Method: "PUT", Path: "/api/v2/image-hosts/{name}", RequiredScopes: []string{"config:manage"}, SafetyLevel: "configuration_write", InputSchema: object(map[string]any{"name": stringProperty("Stable image host name."), "adapter": stringProperty("Image host adapter code."), "enabled": map[string]any{"type": "boolean"}, "priority": map[string]any{"type": "integer"}, "config": map[string]any{"$ref": "#/components/schemas/EndpointConfig"}, "credentials": map[string]any{"type": "object", "additionalProperties": map[string]any{"type": "string"}}}, "name", "adapter", "config")},
		{Name: "create_screenshot_profile", Description: "Create an immutable new screenshot configuration revision.", Method: "POST", Path: "/api/v2/screenshot-profiles", RequiredScopes: []string{"config:manage"}, SafetyLevel: "configuration_write", InputSchema: object(map[string]any{"name": stringProperty("Screenshot profile name."), "enabled": map[string]any{"type": "boolean"}, "config": map[string]any{"$ref": "#/components/schemas/ScreenshotConfig"}}, "name", "config")},
	}
}
