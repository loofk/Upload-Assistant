package server

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/webui"
)

type DatabaseChecker interface {
	Ping(context.Context) error
}

type Dependencies struct {
	Database        DatabaseChecker
	Jobs            JobService
	Auth            TokenAuthenticator
	Rules           RuleService
	RuleCollections RuleCollectionService
	Integrations    IntegrationService
	Downloaders     DownloaderService
	ImageHosts      ImageHostProbeService
	Notifications   NotificationProbeService
	Artifacts       ArtifactContentReader
	Candidates      CandidateService
	Schedules       ScheduleService
	Legacy          LegacyMigrationService
	MediaManagers   MediaManagerService
	Metadata        MetadataProviderService
	AuditLog        AuditLogService
	SiteAccess      SiteAccessService
	LiveReadiness   LiveReadinessService
	Operations      *operations.Store
	Diagnostics     *operations.DiagnosticService
	Backups         *operations.BackupManager
	Tokens          TokenLifecycleService
	LogSink         *operations.AsyncLogSink
	DataDir         string
	DownloadsDir    string
	BackupsDir      string
	Logger          *slog.Logger
	Build           buildinfo.Info
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
	registerAdapterCatalogRoutes(mux)
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
	if deps.RuleCollections != nil {
		registerRuleCollectionRoutes(mux, deps.RuleCollections)
	}
	if deps.Integrations != nil {
		registerIntegrationRoutes(mux, deps.Integrations)
	}
	if deps.Downloaders != nil {
		registerDownloaderRoutes(mux, deps.Downloaders)
	}
	if deps.ImageHosts != nil {
		registerImageHostRoutes(mux, deps.ImageHosts)
	}
	if deps.Notifications != nil {
		registerNotificationProbeRoutes(mux, deps.Notifications)
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
	if deps.Metadata != nil {
		registerMetadataProviderRoutes(mux, deps.Metadata)
	}
	if deps.AuditLog != nil {
		registerAuditLogRoutes(mux, deps.AuditLog)
	}
	if deps.SiteAccess != nil {
		registerSiteAccessRoutes(mux, deps.SiteAccess)
	}
	if deps.LiveReadiness != nil {
		registerLiveReadinessRoutes(mux, deps.LiveReadiness)
	}
	if deps.Operations != nil && deps.Diagnostics != nil && deps.Backups != nil && deps.Tokens != nil {
		registerOperationsRoutes(mux, operationsAPI{
			store: deps.Operations, diagnostics: deps.Diagnostics, backups: deps.Backups, tokens: deps.Tokens,
			ruleAnalyses: newRuleAnalysisCoordinator(10*time.Minute, 16),
			dataDir:      deps.DataDir, downloadsDir: deps.DownloadsDir, backupsDir: deps.BackupsDir,
			version: deps.Build.Version, dropped: func() uint64 {
				if deps.LogSink == nil {
					return 0
				}
				return deps.LogSink.Dropped()
			},
		})
	}
	handler := http.Handler(mux)
	if deps.Operations != nil {
		handler = maintenanceGuard(deps.Operations, handler)
	}
	handler = authenticate(deps.Auth, handler)
	handler = securityHeaders(handler)
	handler = requestLogger(deps.Logger, deps.LogSink, handler)
	return requestCorrelation(handler)
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
		operations.SetActor(r.Context(), "user", principal.UserID)
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
		w.Header().Set("Content-Security-Policy", "default-src 'self'; base-uri 'none'; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data: blob:; object-src 'none'; script-src 'self'; style-src 'self'")
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

type responseRecorder struct {
	http.ResponseWriter
	status          int
	bytes           int64
	errorCode       string
	errorDetail     string
	errorAttributes map[string]any
}

func (r *responseRecorder) SetErrorCode(code string)     { r.errorCode = code }
func (r *responseRecorder) SetErrorDetail(detail string) { r.errorDetail = detail }
func (r *responseRecorder) SetErrorAttributes(attributes map[string]any) {
	r.errorAttributes = attributes
}

func (r *responseRecorder) WriteHeader(status int) {
	if r.status == 0 {
		r.status = status
		r.ResponseWriter.WriteHeader(status)
	}
}
func (r *responseRecorder) Write(body []byte) (int, error) {
	if r.status == 0 {
		r.WriteHeader(http.StatusOK)
	}
	n, err := r.ResponseWriter.Write(body)
	r.bytes += int64(n)
	return n, err
}
func (r *responseRecorder) Flush() {
	if r.status == 0 {
		r.WriteHeader(http.StatusOK)
	}
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (r *responseRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

func requestLogger(logger *slog.Logger, sink *operations.AsyncLogSink, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		recorder := &responseRecorder{ResponseWriter: w}
		next.ServeHTTP(recorder, r)
		if recorder.status == 0 {
			recorder.status = http.StatusOK
		}
		correlation := operations.CorrelationFromContext(r.Context())
		route := r.Pattern
		if route == "" {
			route = normalizedRoute(r.URL.Path)
		}
		duration := time.Since(started).Milliseconds()
		level := "info"
		if recorder.status >= 500 || recorder.errorCode != "" {
			level = "error"
		} else if recorder.status >= 400 {
			level = "warn"
		}
		attributeValues := map[string]any{"user_agent": r.UserAgent()}
		if recorder.errorDetail != "" {
			attributeValues["error_detail"] = recorder.errorDetail
		}
		for key, value := range recorder.errorAttributes {
			attributeValues[key] = value
		}
		attributes, _ := json.Marshal(operations.Redact(attributeValues))
		entry := operations.LogEntry{OccurredAt: time.Now().UTC(), Level: level, Component: "http", Message: "HTTP request", RequestID: correlation.RequestID, TraceID: correlation.TraceID, Method: r.Method, Route: route, StatusCode: recorder.status, DurationMS: duration, ResponseBytes: recorder.bytes, ActorType: correlation.ActorType, ActorID: correlation.ActorID, Attributes: attributes}
		if recorder.status >= 400 || recorder.errorCode != "" {
			entry.ErrorCode = recorder.errorCode
			if entry.ErrorCode == "" {
				entry.ErrorCode = http.StatusText(recorder.status)
			}
		}
		if (r.URL.Path == "/health/live" || r.URL.Path == "/health/ready") && recorder.status < 400 {
			logger.Debug("HTTP health request", "request_id", correlation.RequestID, "trace_id", correlation.TraceID, "method", r.Method, "route", route, "status_code", recorder.status, "duration_ms", duration, "response_bytes", recorder.bytes)
			return
		}
		logger.Log(r.Context(), mapLogLevel(level), "HTTP request", "request_id", correlation.RequestID, "trace_id", correlation.TraceID, "method", r.Method, "route", route, "status_code", recorder.status, "duration_ms", duration, "response_bytes", recorder.bytes, "actor_type", correlation.ActorType, "actor_id", correlation.ActorID)
		if sink != nil {
			sink.Enqueue(entry)
		}
	})
}

func mapLogLevel(level string) slog.Level {
	switch level {
	case "error":
		return slog.LevelError
	case "warn":
		return slog.LevelWarn
	case "debug":
		return slog.LevelDebug
	default:
		return slog.LevelInfo
	}
}

var validRequestID = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)

func requestCorrelation(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
		if !validRequestID.MatchString(requestID) {
			requestID = uuid.NewString()
		}
		traceID := uuid.NewString()
		w.Header().Set("X-Request-ID", requestID)
		w.Header().Set("X-Trace-ID", traceID)
		ctx := operations.WithCorrelation(r.Context(), operations.Correlation{RequestID: requestID, TraceID: traceID})
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func normalizedRoute(path string) string {
	parts := strings.Split(path, "/")
	for index, part := range parts {
		if _, err := uuid.Parse(part); err == nil {
			parts[index] = "{id}"
			continue
		}
		if len(part) > 12 {
			allDigits := true
			for _, char := range part {
				if char < '0' || char > '9' {
					allDigits = false
					break
				}
			}
			if allDigits {
				parts[index] = "{id}"
			}
		}
	}
	return strings.Join(parts, "/")
}

type maintenanceReader interface {
	IsReadOnly(context.Context) (bool, string, error)
}

func maintenanceGuard(reader maintenanceReader, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet || r.Method == http.MethodHead || r.Method == http.MethodOptions {
			next.ServeHTTP(w, r)
			return
		}
		readOnly, reason, err := reader.IsReadOnly(r.Context())
		if err == nil && readOnly {
			writeProblem(w, http.StatusServiceUnavailable, "maintenance_read_only", "service is in a read-only maintenance window: "+reason)
			return
		}
		next.ServeHTTP(w, r)
	})
}
