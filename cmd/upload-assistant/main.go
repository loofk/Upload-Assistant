package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/auditlog"
	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/candidates"
	"github.com/loofk/upload-assistant/v2/internal/clientcli"
	"github.com/loofk/upload-assistant/v2/internal/config"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/legacy"
	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/mediamanagers"
	"github.com/loofk/upload-assistant/v2/internal/metadataproviders"
	"github.com/loofk/upload-assistant/v2/internal/notifications"
	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/readiness"
	"github.com/loofk/upload-assistant/v2/internal/rulecollector"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/schedules"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/server"
	"github.com/loofk/upload-assistant/v2/internal/siteaccess"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/sites/nexusphp"
	"github.com/loofk/upload-assistant/v2/internal/torrentmaker"
	"github.com/loofk/upload-assistant/v2/internal/worker"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
	"golang.org/x/term"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		if !errors.Is(err, clientcli.ErrReported) {
			fmt.Fprintln(os.Stderr, err)
		}
		os.Exit(1)
	}
}

func run(args []string) error {
	command := "serve"
	if len(args) > 0 {
		command = args[0]
		args = args[1:]
	}

	switch command {
	case "serve":
		return serve(args)
	case "migrate":
		return migrate(args)
	case "admin":
		return admin(args)
	case "cli":
		return clientcli.Run(context.Background(), args, clientcli.Streams{
			In: os.Stdin, Out: os.Stdout, Err: os.Stderr, Getenv: os.Getenv, ReadSecret: readAPIToken,
		})
	case "version", "--version", "-version":
		fmt.Println(buildinfo.Current().String())
		return nil
	case "help", "--help", "-h":
		printUsage()
		return nil
	default:
		return fmt.Errorf("unknown command %q", command)
	}
}

func readAPIToken(prompt string) (string, error) {
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return "", errors.New("API token is unavailable without a terminal; set UA_API_TOKEN or use --token-file")
	}
	fmt.Fprint(os.Stderr, prompt)
	token, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stderr)
	if err != nil {
		return "", err
	}
	return string(token), nil
}

func serve(args []string) error {
	flags := flag.NewFlagSet("serve", flag.ContinueOnError)
	listenAddr := flags.String("listen", "", "HTTP listen address (overrides UA_LISTEN_ADDR)")
	if err := flags.Parse(args); err != nil {
		return err
	}

	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	if *listenAddr != "" {
		cfg.ListenAddr = *listenAddr
	}
	logger := newLogger(cfg.LogLevel)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()

	if err := database.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("run database migrations: %w", err)
	}
	if err := config.EnsureDataDirectories(cfg.DataDir); err != nil {
		return fmt.Errorf("prepare data directory: %w", err)
	}
	if err := os.MkdirAll(cfg.BackupsDir, 0o750); err != nil {
		return fmt.Errorf("prepare backups directory: %w", err)
	}
	serviceLock, err := acquireServiceLock(cfg.DataDir)
	if err != nil {
		return err
	}
	defer releaseServiceLock(serviceLock)
	jobStore := workflow.NewStore(pool)
	definition := workflow.RetorrentDefinition()
	workflowID, err := jobStore.EnsureDefinition(ctx, definition)
	if err != nil {
		return fmt.Errorf("ensure retorrent workflow: %w", err)
	}
	jobService := workflow.NewService(jobStore, definition, workflowID)
	candidateDefinition := workflow.DailyCandidatesDefinition()
	candidateWorkflowID, err := jobStore.EnsureDefinition(ctx, candidateDefinition)
	if err != nil {
		return fmt.Errorf("ensure daily candidate workflow: %w", err)
	}
	if err := jobService.RegisterDefinition(candidateDefinition, candidateWorkflowID); err != nil {
		return fmt.Errorf("register daily candidate workflow: %w", err)
	}
	authStore := security.NewAuthStore(pool)
	keyring, createdMasterKey, err := security.LoadOrCreateKeyring(cfg.MasterKeyFile)
	if err != nil {
		return fmt.Errorf("load master keyring: %w", err)
	}
	if createdMasterKey {
		logger.Warn("created persistent master key file; include it in encrypted configuration backups", "path", cfg.MasterKeyFile)
	}
	secretStore := security.NewSecretStore(pool, keyring)
	operationsStore := operations.NewStore(pool)
	ruleStore, err := rules.NewStore(pool, cfg.DataDir)
	if err != nil {
		return fmt.Errorf("initialize rule store: %w", err)
	}
	logSink := operations.NewAsyncLogSink(operationsStore, logger, 2048)
	go logSink.Run(ctx)
	diagnosticService := &operations.DiagnosticService{Store: operationsStore, Secrets: secretStore, RuleSource: ruleStore}
	go diagnosticService.Run(ctx)
	backupManager := &operations.BackupManager{
		Store: operationsStore, DatabaseURL: cfg.DatabaseURL, DataDir: cfg.DataDir, BackupsDir: cfg.BackupsDir,
		MasterKeyFile: cfg.MasterKeyFile, Version: buildinfo.Current().Version, PgDumpBinary: cfg.PgDumpBinary,
		PgRestoreBinary: cfg.PgRestoreBinary, AgeBinary: cfg.AgeBinary, AgeKeygenBinary: cfg.AgeKeygenBinary,
	}
	go backupManager.RunScheduler(ctx)
	go runOperationsRetention(ctx, operationsStore, cfg.DataDir, cfg.DownloadsDir, cfg.BackupsDir, buildinfo.Current().Version, logger)
	integrationStore := integrations.NewStore(pool, secretStore)
	siteAccessStore := siteaccess.NewStore(pool)
	ruleCollectionService := rulecollector.NewService(pool, cfg.DataDir, integrationStore, siteAccessStore, diagnosticService, ruleStore)
	go ruleCollectionService.Run(ctx)
	mediaManager := mediamanagers.NewManager(integrationStore, nil)
	metadataProvider := metadataproviders.NewManager(integrationStore, nil)
	auditLogStore := auditlog.NewStore(pool)
	legacyService, err := legacy.NewService(pool, secretStore, integrationStore, cfg.LegacyDir, logger)
	if err != nil {
		return fmt.Errorf("initialize legacy migration service: %w", err)
	}
	downloaderManager := downloaders.NewManager(integrationStore)
	imageHostManager := imagehosts.NewManager(integrationStore, nil)
	mteamClient := mteam.NewClient(integrationStore, siteAccessStore, nil)
	liveReadiness := readiness.NewService(ruleStore, integrationStore, readiness.Runtime{
		MediaInfoBinary: cfg.MediaInfoBinary, BDInfoBinary: cfg.BDInfoBinary, FFmpegBinary: cfg.FFmpegBinary,
		FFprobeBinary: cfg.FFprobeBinary, MkbrrBinary: cfg.MkbrrBinary, DownloadsDir: "/downloads",
	})
	artifactStore, err := artifacts.NewLocalStore(cfg.DataDir)
	if err != nil {
		return fmt.Errorf("initialize artifact store: %w", err)
	}
	candidateStore := candidates.NewStore(pool)
	scheduleStore := schedules.NewStore(pool)
	sourceRegistry, err := buildSourceRegistry(integrationStore, siteAccessStore)
	if err != nil {
		return fmt.Errorf("initialize source adapters: %w", err)
	}
	targetPackageRegistry, err := sites.NewTargetPackageRegistry(mteam.NewPackageAdapter())
	if err != nil {
		return fmt.Errorf("initialize target package adapters: %w", err)
	}
	targetDuplicateRegistry, err := sites.NewTargetDuplicateRegistry(mteamClient)
	if err != nil {
		return fmt.Errorf("initialize target duplicate adapters: %w", err)
	}
	targetTorrentRegistry, err := sites.NewTargetTorrentRegistry(mteam.NewTorrentAdapter())
	if err != nil {
		return fmt.Errorf("initialize target torrent adapters: %w", err)
	}
	targetUploadRegistry, err := sites.NewTargetUploadRegistry(mteamClient)
	if err != nil {
		return fmt.Errorf("initialize target upload adapters: %w", err)
	}
	targetTorrentDownloadRegistry, err := sites.NewTargetTorrentDownloadRegistry(mteamClient)
	if err != nil {
		return fmt.Errorf("initialize target torrent download adapters: %w", err)
	}
	hostname, _ := os.Hostname()
	workerID := fmt.Sprintf("%s-%d", hostname, os.Getpid())
	jobRunner := worker.New(
		jobService, workerID, logger,
		worker.WithOperationalLogs(logSink),
		worker.WithRuleProvider(ruleStore),
		worker.WithSourceAdapters(sourceRegistry, artifactStore),
		worker.WithDownloader(downloaderManager, artifactStore),
		worker.WithMetadata(artifactStore),
		worker.WithMetadataProviders(metadataProvider, artifactStore),
		worker.WithMediaInspection(
			media.NewMediaInfo(cfg.MediaInfoBinary, 2*time.Minute),
			media.NewBDInfo(cfg.BDInfoBinary, filepath.Join(cfg.DataDir, "tmp"), 15*time.Minute),
			artifactStore,
		),
		worker.WithScreenshots(
			integrationStore,
			media.NewFFmpegScreenshots(cfg.FFmpegBinary, cfg.FFprobeBinary, 5*time.Minute),
			artifactStore,
		),
		worker.WithImageHosts(imageHostManager, artifactStore),
		worker.WithTargetPackages(targetPackageRegistry, artifactStore, ruleStore),
		worker.WithTargetDuplicateChecks(targetDuplicateRegistry, artifactStore),
		worker.WithTargetTorrents(
			targetTorrentRegistry,
			torrentmaker.NewMkbrr(cfg.MkbrrBinary, filepath.Join(cfg.DataDir, "tmp"), 0),
			artifactStore,
		),
		worker.WithTargetUploads(targetUploadRegistry, targetDuplicateRegistry, ruleStore, artifactStore),
		worker.WithTargetTorrentDownloads(targetTorrentDownloadRegistry, artifactStore),
		worker.WithTargetInjection(downloaderManager, artifactStore),
		worker.WithTargetSeedVerification(downloaderManager, artifactStore),
		worker.WithSummary(artifactStore),
		worker.WithDailyCandidates(ruleStore, sourceRegistry, targetDuplicateRegistry, candidateStore, artifactStore),
	)
	go jobRunner.Run(ctx)
	dailyScheduler := schedules.NewRunner(scheduleStore, jobService, workerID+"-scheduler", logger)
	go dailyScheduler.Run(ctx)
	notificationStore := notifications.NewStore(pool, integrationStore)
	notificationDispatcher := notifications.NewDispatcher(notificationStore, workerID+"-notifications", nil, logger)
	notificationProber := notifications.NewProber(notificationStore, notificationDispatcher, workerID+"-notification-probes")
	go notificationDispatcher.Run(ctx)
	go runLegacyArchiveCleanup(ctx, legacyService, logger)

	handler := server.New(server.Dependencies{
		Database:        pool,
		Jobs:            jobService,
		Auth:            authStore,
		Rules:           ruleStore,
		RuleCollections: ruleCollectionService,
		Integrations:    integrationStore,
		Downloaders:     downloaderManager,
		ImageHosts:      imageHostManager,
		Notifications:   notificationProber,
		Artifacts:       artifactStore,
		Candidates:      candidateStore,
		Schedules:       scheduleStore,
		Legacy:          legacyService,
		MediaManagers:   mediaManager,
		Metadata:        metadataProvider,
		AuditLog:        auditLogStore,
		SiteAccess:      siteAccessStore,
		LiveReadiness:   liveReadiness,
		Operations:      operationsStore,
		Diagnostics:     diagnosticService,
		Backups:         backupManager,
		Tokens:          authStore,
		LogSink:         logSink,
		DataDir:         cfg.DataDir,
		DownloadsDir:    cfg.DownloadsDir,
		BackupsDir:      cfg.BackupsDir,
		Logger:          logger,
		Build:           buildinfo.Current(),
	})
	httpServer := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		logger.Info("HTTP server started", "listen_addr", cfg.ListenAddr, "version", buildinfo.Current().Version)
		errCh <- httpServer.ListenAndServe()
	}()

	select {
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return fmt.Errorf("serve HTTP: %w", err)
	case <-ctx.Done():
		logger.Info("shutdown requested")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("shutdown HTTP server: %w", err)
	}
	return nil
}

func runLegacyArchiveCleanup(ctx context.Context, service *legacy.Service, logger *slog.Logger) {
	cleanup := func() {
		count, err := service.CleanupExpired(ctx, workflow.Actor{Type: "system", ID: "legacy-retention"})
		if err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("legacy archive retention cleanup failed")
			return
		}
		if count > 0 {
			logger.Info("expired encrypted legacy archives deleted", "count", count)
		}
	}
	cleanup()
	ticker := time.NewTicker(24 * time.Hour)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			cleanup()
		}
	}
}

func runOperationsRetention(ctx context.Context, store *operations.Store, dataDir, downloadsDir, backupsDir, version string, logger *slog.Logger) {
	cleanup := func() {
		settings, err := store.GetSettings(ctx)
		if err != nil {
			if !errors.Is(err, context.Canceled) {
				logger.Error("operations retention settings unavailable", "error", err)
			}
			return
		}
		count, err := store.PurgeExpired(ctx, settings.LogRetentionDays, settings.DiagnosticRetentionDays)
		if err != nil {
			if !errors.Is(err, context.Canceled) {
				logger.Error("operations retention cleanup failed", "error", err)
			}
			return
		}
		if count > 0 {
			logger.Info("expired operational logs deleted", "count", count)
		}
		if err := store.EvaluateCapacity(ctx, dataDir, downloadsDir, backupsDir, version); err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("capacity evaluation failed", "error", err)
		}
	}
	cleanup()
	ticker := time.NewTicker(time.Hour)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			cleanup()
		}
	}
}

func buildSourceRegistry(provider nexusphp.RuntimeSiteProvider, accessGate sites.AccessGate) (*sites.Registry, error) {
	adapters := make([]sites.SourceAdapter, 0, len(nexusphp.ProductionProfiles))
	for _, profile := range nexusphp.ProductionProfiles {
		adapter, err := nexusphp.New(profile, provider, accessGate, nil)
		if err != nil {
			return nil, err
		}
		adapters = append(adapters, adapter)
	}
	return sites.NewRegistry(adapters...)
}

func migrate(args []string) error {
	flags := flag.NewFlagSet("migrate", flag.ContinueOnError)
	if err := flags.Parse(args); err != nil {
		return err
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()
	if err := database.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("run database migrations: %w", err)
	}
	fmt.Println("database migrations are up to date")
	return nil
}

func admin(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: upload-assistant admin <bootstrap|token issue|rules import|llm probe> [options]")
	}
	switch args[0] {
	case "bootstrap":
		return adminBootstrap(args[1:])
	case "token":
		if len(args) < 2 || args[1] != "issue" {
			return errors.New("usage: upload-assistant admin token issue --username <name> --name <token-name> --confirm")
		}
		return adminIssueToken(args[2:])
	case "backup":
		if len(args) < 2 || args[1] != "restore" {
			return errors.New("usage: upload-assistant admin backup restore --bundle FILE --identity FILE --confirm")
		}
		return adminRestoreBackup(args[2:])
	case "llm":
		if len(args) < 2 {
			return errors.New("usage: upload-assistant admin llm <probe|analyze-rule> [options]")
		}
		if args[1] == "probe" {
			return adminProbeLLM(args[2:])
		}
		if args[1] == "analyze-rule" {
			return adminAnalyzeRule(args[2:])
		}
		return errors.New("usage: upload-assistant admin llm <probe|analyze-rule> [options]")
	case "rules":
		if len(args) < 2 || args[1] != "import" {
			return errors.New("usage: upload-assistant admin rules import --file FILE --confirm")
		}
		return adminImportRule(args[2:])
	default:
		return errors.New("usage: upload-assistant admin <bootstrap|token issue|rules import|llm probe> [options]")
	}
}

func adminImportRule(args []string) error {
	flags := flag.NewFlagSet("admin rules import", flag.ContinueOnError)
	filename := flags.String("file", "", "validated site-rule Markdown file")
	confirm := flags.Bool("confirm", false, "confirm importing a new immutable draft revision")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*filename) == "" || !*confirm {
		return errors.New("local rule import requires --file and --confirm")
	}
	info, err := os.Stat(*filename)
	if err != nil {
		return fmt.Errorf("stat rule Markdown: %w", err)
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > rules.MaxMarkdownBytes {
		return fmt.Errorf("rule Markdown must be a regular file between 1 and %d bytes", rules.MaxMarkdownBytes)
	}
	body, err := os.ReadFile(*filename)
	if err != nil {
		return fmt.Errorf("read rule Markdown: %w", err)
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()
	if err := database.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("run database migrations: %w", err)
	}
	store, err := rules.NewStore(pool, cfg.DataDir)
	if err != nil {
		return fmt.Errorf("initialize rule store: %w", err)
	}
	revision, err := store.Import(ctx, body, workflow.Actor{Type: "admin_cli"})
	if err != nil {
		return fmt.Errorf("import rule draft: %w", err)
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{
		"ok": true, "status": "draft", "revision_id": revision.ID, "site_code": revision.SiteCode,
		"revision": revision.Revision, "fingerprint": revision.Fingerprint,
		"summary": "immutable rule draft imported; no review, approval, activation, tracker call, or upload was performed",
	})
}

func adminAnalyzeRule(args []string) error {
	flags := flag.NewFlagSet("admin llm analyze-rule", flag.ContinueOnError)
	providerID := flags.String("provider-id", "", "configured provider UUID")
	revisionID := flags.String("revision-id", "", "immutable site-rule revision UUID")
	stream := flags.Bool("stream", false, "use SSE streaming transport for an explicit compatibility test")
	confirm := flags.Bool("confirm-external", false, "confirm sending the revision text to the configured provider")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*providerID) == "" || strings.TrimSpace(*revisionID) == "" || !*confirm {
		return errors.New("rule analysis requires --provider-id, --revision-id, and --confirm-external")
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 11*time.Minute)
	defer cancel()
	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()
	keyring, err := security.LoadKeyring(cfg.MasterKeyFile)
	if err != nil {
		return fmt.Errorf("load configured master keyring: %w", err)
	}
	ruleStore, err := rules.NewStore(pool, cfg.DataDir)
	if err != nil {
		return fmt.Errorf("initialize rule store: %w", err)
	}
	service := &operations.DiagnosticService{
		Store: operations.NewStore(pool), Secrets: security.NewSecretStore(pool, keyring), RuleSource: ruleStore,
	}
	traceID := uuid.NewString()
	ctx = operations.WithCorrelation(ctx, operations.Correlation{RequestID: "admin-llm-rule-analysis", TraceID: traceID, ActorType: "admin_cli"})
	result, err := service.AnalyzeRuleText(ctx, operations.RuleAnalysisInput{ProviderID: strings.TrimSpace(*providerID), SourceRevisionID: strings.TrimSpace(*revisionID), StreamingTest: *stream}, security.Principal{}, traceID)
	if err != nil {
		return fmt.Errorf("analyze configured rule revision: %w", err)
	}
	draftDigest := sha256.Sum256([]byte(result.DraftMarkdown))
	return json.NewEncoder(os.Stdout).Encode(map[string]any{
		"ok": true, "status": "draft_ready", "provider_id": result.ProviderID, "model": result.Model,
		"reasoning_effort": result.ReasoningEffort, "source_revision_id": result.SourceRevisionID,
		"source_sha256": result.SourceSHA256, "draft_sha256": hex.EncodeToString(draftDigest[:]),
		"confidence": result.Confidence, "warnings": result.Warnings, "external_calls_performed": true,
		"streaming": *stream, "stream_metrics": result.StreamMetrics,
	})
}

func adminProbeLLM(args []string) error {
	flags := flag.NewFlagSet("admin llm probe", flag.ContinueOnError)
	providerID := flags.String("provider-id", "", "configured provider UUID")
	stage := flags.String("stage", operations.ProviderProbeStageInference, "catalog or inference")
	confirm := flags.Bool("confirm-external", false, "confirm contacting the configured external provider")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*providerID) == "" || !*confirm {
		return errors.New("provider probe requires --provider-id and --confirm-external")
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 11*time.Minute)
	defer cancel()
	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()
	keyring, err := security.LoadKeyring(cfg.MasterKeyFile)
	if err != nil {
		return fmt.Errorf("load configured master keyring: %w", err)
	}
	store := operations.NewStore(pool)
	service := &operations.DiagnosticService{Store: store, Secrets: security.NewSecretStore(pool, keyring)}
	ctx = operations.WithCorrelation(ctx, operations.Correlation{RequestID: "admin-llm-probe", ActorType: "admin_cli"})
	result, probeErr := service.Probe(ctx, strings.TrimSpace(*providerID), strings.TrimSpace(*stage))
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{
		"ok": probeErr == nil, "status": result.Status, "provider_id": strings.TrimSpace(*providerID),
		"probe": result, "external_calls_performed": result.ExternalCallPerformed,
	})
	if probeErr != nil {
		return fmt.Errorf("probe configured provider: %w", probeErr)
	}
	return nil
}

func adminRestoreBackup(args []string) error {
	flags := flag.NewFlagSet("admin backup restore", flag.ContinueOnError)
	bundle := flags.String("bundle", "", "encrypted age backup bundle")
	identity := flags.String("identity", "", "offline age identity file")
	confirm := flags.Bool("confirm", false, "confirm stopped-service restore")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if !*confirm || *bundle == "" || *identity == "" {
		return errors.New("offline backup restore requires --bundle, --identity, and --confirm")
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	lock, err := acquireServiceLock(cfg.DataDir)
	if err != nil {
		return fmt.Errorf("restore refused while service is running: %w", err)
	}
	defer releaseServiceLock(lock)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	err = operations.RestoreOffline(ctx, operations.RestoreOptions{BundlePath: *bundle, IdentityFile: *identity, DatabaseURL: cfg.DatabaseURL, DataDir: cfg.DataDir, MasterKeyFile: cfg.MasterKeyFile, AgeBinary: cfg.AgeBinary, PgRestoreBinary: cfg.PgRestoreBinary, ExpectedVersion: buildinfo.Current().Version})
	if err != nil {
		return fmt.Errorf("restore encrypted backup: %w", err)
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true, "status": "complete", "summary": "encrypted backup restored after manifest, version, and internal hash validation"})
}

func acquireServiceLock(dataDir string) (*os.File, error) {
	file, err := os.OpenFile(filepath.Join(dataDir, ".upload-assistant-service.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		return nil, errors.New("service lock is already held")
	}
	return file, nil
}
func releaseServiceLock(file *os.File) {
	if file == nil {
		return
	}
	_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
	_ = file.Close()
}

func adminBootstrap(args []string) error {
	flags := flag.NewFlagSet("admin bootstrap", flag.ContinueOnError)
	username := flags.String("username", "admin", "administrator username")
	if err := flags.Parse(args); err != nil {
		return err
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	password, err := readPassword()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()
	if err := database.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("run database migrations: %w", err)
	}
	result, err := security.NewAuthStore(pool).BootstrapAdmin(ctx, *username, password)
	if err != nil {
		return fmt.Errorf("bootstrap administrator: %w", err)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(map[string]any{"ok": true, "status": "complete", "admin": result})
}

func adminIssueToken(args []string) error {
	flags := flag.NewFlagSet("admin token issue", flag.ContinueOnError)
	username := flags.String("username", "admin", "existing administrator username")
	name := flags.String("name", "web-recovery", "auditable API token name")
	confirm := flags.Bool("confirm", false, "confirm issuing a new administrator API token")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if !*confirm {
		return errors.New("issuing an administrator API token requires --confirm")
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	pool, err := database.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer pool.Close()
	if err := database.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("run database migrations: %w", err)
	}
	result, err := security.NewAuthStore(pool).IssueAdminToken(ctx, *username, *name)
	if err != nil {
		return fmt.Errorf("issue administrator API token: %w", err)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(map[string]any{
		"ok": true, "status": "complete", "api_token": result,
		"summary": "API token issued once; store it in a password manager or a mode-0600 token file",
	})
}

func readPassword() (string, error) {
	if term.IsTerminal(int(os.Stdin.Fd())) {
		fmt.Fprint(os.Stderr, "Administrator password: ")
		password, err := term.ReadPassword(int(os.Stdin.Fd()))
		fmt.Fprintln(os.Stderr)
		if err != nil {
			return "", fmt.Errorf("read administrator password: %w", err)
		}
		return string(password), nil
	}
	reader := bufio.NewReaderSize(os.Stdin, 4096)
	password, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) && len(password) == 0 {
		return "", fmt.Errorf("read administrator password: %w", err)
	}
	password = strings.TrimRight(password, "\r\n")
	if password == "" {
		return "", errors.New("administrator password is required on stdin")
	}
	return password, nil
}

func newLogger(level string) *slog.Logger {
	var slogLevel slog.Level
	switch level {
	case "debug":
		slogLevel = slog.LevelDebug
	case "warn":
		slogLevel = slog.LevelWarn
	case "error":
		slogLevel = slog.LevelError
	default:
		slogLevel = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slogLevel}))
}

func printUsage() {
	fmt.Println(`Upload-Assistant v2

Usage:
  upload-assistant serve [--listen address]
  upload-assistant migrate
  upload-assistant admin bootstrap --username <name>
  upload-assistant admin token issue --username <name> --name <token-name> --confirm
  upload-assistant admin rules import --file FILE --confirm
  upload-assistant admin llm probe --provider-id UUID --stage <catalog|inference> --confirm-external
  upload-assistant admin llm analyze-rule --provider-id UUID --revision-id UUID [--stream] --confirm-external
  upload-assistant admin backup restore --bundle FILE --identity FILE --confirm
  upload-assistant cli [--api-url URL] [--token-file FILE] <command>
  upload-assistant version`)
}
