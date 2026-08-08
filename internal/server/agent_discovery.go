package server

import (
	"net/http"

	"github.com/loofk/upload-assistant/v2/internal/agentskill"
)

const (
	agentDiscoveryPath = "/.well-known/upload-assistant.json"
	agentSkillPath     = "/.well-known/upload-assistant/SKILL.md"
)

func registerAgentDiscoveryRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET "+agentDiscoveryPath, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Cache-Control", "public, max-age=60")
		writeJSON(w, http.StatusOK, map[string]any{
			"schema_version": 1,
			"kind":           "upload-assistant.agent-discovery.v1",
			"name":           "upload-assistant",
			"skill_format":   "AgentSkills/SKILL.md",
			"skill_url":      agentSkillPath,
			"openapi_url":    "/openapi.json",
			"tools_url":      "/api/v2/tools",
			"health_url":     "/health/ready",
			"authentication": map[string]any{
				"type":             "http_bearer",
				"header":           "Authorization",
				"required_prefix":  "Bearer ",
				"protected_prefix": "/api/v2/",
			},
			"installation": map[string]any{
				"openclaw_project_path": ".agents/skills/upload-assistant",
				"hermes_skill_url":      agentSkillPath,
			},
			"compatibility": []string{"OpenClaw", "Hermes Agent"},
			"safety": map[string]any{
				"live_upload_requires":   []string{"accept_rules", "confirm_upload"},
				"mandatory_gates":        []string{"rules", "manual_obligations", "duplicate_check", "upload_confirmation", "seeding"},
				"credentials_write_only": true,
			},
		})
	})
	mux.HandleFunc("GET "+agentSkillPath, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/markdown; charset=utf-8")
		w.Header().Set("Cache-Control", "public, max-age=60")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(agentskill.Markdown())
	})
}
