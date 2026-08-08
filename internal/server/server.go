package server

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/webui"
)

type DatabaseChecker interface {
	Ping(context.Context) error
}

type Dependencies struct {
	Database      DatabaseChecker
	Jobs          JobService
	Auth          TokenAuthenticator
	Rules         RuleService
	Integrations  IntegrationService
	Downloaders   DownloaderService
	Artifacts     ArtifactContentReader
	Candidates    CandidateService
	Schedules     ScheduleService
	Legacy        LegacyMigrationService
	MediaManagers MediaManagerService
	AuditLog      AuditLogService
	LiveReadiness LiveReadinessService
	DataDir       string
	Logger        *slog.Logger
	Build         buildinfo.Info
}

type healthResponse struct {
	OK      bool              `json:"ok"`
	Status  string            `json:"status"`
	Service string            `json:"service"`
	Version string            `json:"version"`
	Checks  map[string]string `json:"checks,omitempty"`
}

func New(deps Dependencies) http.Handler {
	if deps.Logger == nil {
		deps.Logger = slog.New(slog.NewTextHandler(os.Stderr, nil))
	}
	mux := http.NewServeMux()
	webui.Register(mux)
	registerDocumentationRoutes(mux)
	registerAgentDiscoveryRoutes(mux)
	mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, healthResponse{
			OK: true, Status: "alive", Service: "upload-assistant", Version: deps.Build.Version,
		})
	})
	mux.HandleFunc("GET /health/ready", func(w http.ResponseWriter, r *http.Request) {
		checks := map[string]string{"database": "ready", "data_dir": "ready"}
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		if deps.Database == nil || deps.Database.Ping(ctx) != nil {
			checks["database"] = "failed"
		}
		if info, err := os.Stat(deps.DataDir); err != nil || !info.IsDir() {
			checks["data_dir"] = "failed"
		}
		ready := checks["database"] == "ready" && checks["data_dir"] == "ready"
		statusCode := http.StatusOK
		status := "ready"
		if !ready {
			statusCode = http.StatusServiceUnavailable
			status = "not_ready"
		}
		writeJSON(w, statusCode, healthResponse{
			OK: ready, Status: status, Service: "upload-assistant", Version: deps.Build.Version, Checks: checks,
		})
	})
	mux.HandleFunc("GET /api/v2/version", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"ok": true, "status": "ready", "build": deps.Build,
		})
	})
	if deps.Jobs != nil {
		registerJobRoutes(mux, deps.Jobs, deps.Artifacts)
	}
	if deps.Rules != nil {
		registerRuleRoutes(mux, deps.Rules)
	}
	if deps.Integrations != nil {
		registerIntegrationRoutes(mux, deps.Integrations)
	}
	if deps.Downloaders != nil {
		registerDownloaderRoutes(mux, deps.Downloaders)
	}
	if deps.Candidates != nil && deps.Jobs != nil {
		registerCandidateRoutes(mux, deps.Candidates, deps.Jobs)
	}
	if deps.Schedules != nil {
		registerScheduleRoutes(mux, deps.Schedules)
	}
	if deps.Legacy != nil {
		registerLegacyMigrationRoutes(mux, deps.Legacy)
	}
	if deps.MediaManagers != nil {
		registerMediaManagerRoutes(mux, deps.MediaManagers)
	}
	if deps.AuditLog != nil {
		registerAuditLogRoutes(mux, deps.AuditLog)
	}
	if deps.LiveReadiness != nil {
		registerLiveReadinessRoutes(mux, deps.LiveReadiness)
	}
	return requestLogger(deps.Logger, securityHeaders(authenticate(deps.Auth, mux)))
}

type TokenAuthenticator interface {
	AuthenticateToken(context.Context, string) (security.Principal, error)
}

func authenticate(authenticator TokenAuthenticator, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if isPublicPath(r.URL.Path) {
			next.ServeHTTP(w, r)
			return
		}
		if authenticator == nil {
			writeProblem(w, http.StatusServiceUnavailable, "authentication_unavailable", "authentication is not configured")
			return
		}
		scheme, token, found := strings.Cut(strings.TrimSpace(r.Header.Get("Authorization")), " ")
		if !found || !strings.EqualFold(scheme, "Bearer") || strings.TrimSpace(token) == "" {
			w.Header().Set("WWW-Authenticate", `Bearer realm="upload-assistant"`)
			writeProblem(w, http.StatusUnauthorized, "authentication_required", "a bearer API token is required")
			return
		}
		principal, err := authenticator.AuthenticateToken(r.Context(), strings.TrimSpace(token))
		if err != nil {
			w.Header().Set("WWW-Authenticate", `Bearer realm="upload-assistant", error="invalid_token"`)
			writeProblem(w, http.StatusUnauthorized, "invalid_token", "the bearer API token is invalid, expired, or revoked")
			return
		}
		next.ServeHTTP(w, r.WithContext(security.WithPrincipal(r.Context(), principal)))
	})
}

func isPublicPath(path string) bool {
	return path == "/" || path == "/app" || strings.HasPrefix(path, "/app/") || strings.HasPrefix(path, "/assets/") || path == "/favicon.svg" ||
		path == "/health/live" || path == "/health/ready" || path == "/api/v2/version" || path == "/openapi.json" ||
		path == agentDiscoveryPath || path == agentSkillPath
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", "default-src 'self'; base-uri 'none'; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'")
		w.Header().Set("Cross-Origin-Opener-Policy", "same-origin")
		w.Header().Set("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func requestLogger(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		next.ServeHTTP(w, r)
		logger.Info("HTTP request", "method", r.Method, "path", r.URL.Path, "duration_ms", time.Since(started).Milliseconds())
	})
}
