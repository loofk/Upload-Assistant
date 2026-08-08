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
)

type DatabaseChecker interface {
	Ping(context.Context) error
}

type Dependencies struct {
	Database     DatabaseChecker
	Jobs         JobService
	Auth         TokenAuthenticator
	Rules        RuleService
	Integrations IntegrationService
	Downloaders  DownloaderService
	DataDir      string
	Logger       *slog.Logger
	Build        buildinfo.Info
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
	registerDocumentationRoutes(mux)
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
		registerJobRoutes(mux, deps.Jobs)
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
	return requestLogger(deps.Logger, authenticate(deps.Auth, mux))
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
	return path == "/health/live" || path == "/health/ready" || path == "/api/v2/version" || path == "/openapi.json"
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
