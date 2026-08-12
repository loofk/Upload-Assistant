package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/siteaccess"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type SiteAccessService interface {
	GetPolicy(context.Context, string) (siteaccess.EffectivePolicy, error)
	UpsertPolicy(context.Context, string, siteaccess.PolicyInput, workflow.Actor) (siteaccess.EffectivePolicy, error)
}

type siteAccessAPI struct{ service SiteAccessService }

func registerSiteAccessRoutes(mux *http.ServeMux, service SiteAccessService) {
	api := siteAccessAPI{service: service}
	mux.HandleFunc("GET /api/v2/sites/{site_code}/access-policy", api.getPolicy)
	mux.HandleFunc("PUT /api/v2/sites/{site_code}/access-policy", api.putPolicy)
}

func (api siteAccessAPI) getPolicy(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	policy, err := api.service.GetPolicy(r.Context(), siteAccessCode(r))
	if err != nil {
		writeSiteAccessError(w, err)
		return
	}
	writeSiteAccessPolicy(w, policy)
}

func (api siteAccessAPI) putPolicy(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var input siteaccess.PolicyInput
	if err := decodeJSON(w, r, &input); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	policy, err := api.service.UpsertPolicy(r.Context(), siteAccessCode(r), input, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeSiteAccessError(w, err)
		return
	}
	writeSiteAccessPolicy(w, policy)
}

func writeSiteAccessPolicy(w http.ResponseWriter, policy siteaccess.EffectivePolicy) {
	status := "ready"
	ok := len(policy.Blockers) == 0
	if !ok {
		status = "blocked"
	}
	nextActions := []map[string]any{}
	for _, blocker := range policy.Blockers {
		switch blocker.Code {
		case "site_access_policy_required", "site_access_disabled":
			nextActions = append(nextActions, map[string]any{"action": "configure_site_access_policy", "site_code": policy.SiteCode})
		case "site_access_rule_required", "site_access_rule_v2_required", "site_access_rule_invalid":
			nextActions = append(nextActions, map[string]any{"action": "review_and_activate_site_rule_v2", "site_code": policy.SiteCode})
		case "site_disabled":
			nextActions = append(nextActions, map[string]any{"action": "enable_site", "site_code": policy.SiteCode})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": ok, "status": status, "site_code": policy.SiteCode, "access_policy": policy,
		"blockers": policy.Blockers, "next_actions": nextActions,
	})
}

func writeSiteAccessError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, siteaccess.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "site_not_found", "site is not configured")
	case errors.Is(err, siteaccess.ErrValidation):
		writeProblem(w, http.StatusBadRequest, "site_access_policy_invalid", err.Error())
	default:
		writeProblem(w, http.StatusInternalServerError, "site_access_failed", "site access policy operation failed")
	}
}

func siteAccessCode(r *http.Request) string {
	return strings.ToUpper(strings.TrimSpace(r.PathValue("site_code")))
}
