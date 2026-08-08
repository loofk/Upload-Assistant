package main

import (
	"bufio"
	"context"
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
	"github.com/loofk/upload-assistant/v2/internal/notifications"
	"github.com/loofk/upload-assistant/v2/internal/readiness"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/schedules"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/server"
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
	integrationStore := integrations.NewStore(pool, secretStore)
	mediaManager := mediamanagers.NewManager(integrationStore, nil)
	auditLogStore := auditlog.NewStore(pool)
	legacyService, err := legacy.NewService(pool, secretStore, integrationStore, cfg.LegacyDir, logger)
	if err != nil {
		return fmt.Errorf("initialize legacy migration service: %w", err)
	}
	downloaderManager := downloaders.NewManager(integrationStore)
	imageHostManager := imagehosts.NewManager(integrationStore, nil)
	mteamClient := mteam.NewClient(integrationStore, nil)
	ruleStore, err := rules.NewStore(pool, cfg.DataDir)
	if err != nil {
		return fmt.Errorf("initialize rule store: %w", err)
	}
	liveReadiness := readiness.NewService(ruleStore, integrationStore, readiness.Runtime{
		MediaInfoBinary: cfg.MediaInfoBinary, FFmpegBinary: cfg.FFmpegBinary,
		FFprobeBinary: cfg.FFprobeBinary, MkbrrBinary: cfg.MkbrrBinary, DownloadsDir: "/downloads",
	})
	artifactStore, err := artifacts.NewLocalStore(cfg.DataDir)
	if err != nil {
		return fmt.Errorf("initialize artifact store: %w", err)
	}
	candidateStore := candidates.NewStore(pool)
	scheduleStore := schedules.NewStore(pool)
	sourceRegistry, err := buildSourceRegistry(integrationStore)
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
		worker.WithRuleProvider(ruleStore),
		worker.WithSourceAdapters(sourceRegistry, artifactStore),
		worker.WithDownloader(downloaderManager, artifactStore),
		worker.WithMetadata(artifactStore),
		worker.WithMediaInfo(media.NewMediaInfo(cfg.MediaInfoBinary, 2*time.Minute), artifactStore),
		worker.WithScreenshots(
			integrationStore,
			media.NewFFmpegScreenshots(cfg.FFmpegBinary, cfg.FFprobeBinary, 5*time.Minute),
			artifactStore,
		),
		worker.WithImageHosts(imageHostManager, artifactStore),
		worker.WithTargetPackages(targetPackageRegistry, artifactStore),
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
	go notificationDispatcher.Run(ctx)
	go runLegacyArchiveCleanup(ctx, legacyService, logger)

	handler := server.New(server.Dependencies{
		Database:      pool,
		Jobs:          jobService,
		Auth:          authStore,
		Rules:         ruleStore,
		Integrations:  integrationStore,
		Downloaders:   downloaderManager,
		Artifacts:     artifactStore,
		Candidates:    candidateStore,
		Schedules:     scheduleStore,
		Legacy:        legacyService,
		MediaManagers: mediaManager,
		AuditLog:      auditLogStore,
		LiveReadiness: liveReadiness,
		DataDir:       cfg.DataDir,
		Logger:        logger,
		Build:         buildinfo.Current(),
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

func buildSourceRegistry(provider nexusphp.RuntimeSiteProvider) (*sites.Registry, error) {
	adapters := make([]sites.SourceAdapter, 0, len(nexusphp.ProductionProfiles))
	for _, profile := range nexusphp.ProductionProfiles {
		adapter, err := nexusphp.New(profile, provider, nil)
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
	if len(args) == 0 || args[0] != "bootstrap" {
		return errors.New("usage: upload-assistant admin bootstrap --username <name>")
	}
	flags := flag.NewFlagSet("admin bootstrap", flag.ContinueOnError)
	username := flags.String("username", "admin", "administrator username")
	if err := flags.Parse(args[1:]); err != nil {
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
  upload-assistant cli [--api-url URL] [--token-file FILE] <command>
  upload-assistant version`)
}
