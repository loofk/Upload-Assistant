package clientcli

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/apiclient"
)

var ErrReported = errors.New("CLI error was reported as JSON")

type Streams struct {
	In         io.Reader
	Out        io.Writer
	Err        io.Writer
	Getenv     func(string) string
	ReadSecret func(string) (string, error)
}

type options struct {
	apiURL            string
	tokenFile         string
	timeout           time.Duration
	compact           bool
	allowInsecureHTTP bool
}

type runner struct {
	client  *apiclient.Client
	streams Streams
	compact bool
}

func Run(ctx context.Context, args []string, streams Streams) error {
	streams = normalizedStreams(streams)
	global, command, err := parseGlobal(args, streams.Getenv)
	if err != nil {
		return reportError(streams.Out, global.compact, err)
	}
	if len(command) == 0 || command[0] == "help" || command[0] == "--help" || command[0] == "-h" {
		_, err := io.WriteString(streams.Out, usage())
		return err
	}
	token := ""
	if command[0] != "health" {
		token, err = loadToken(global.tokenFile, streams)
		if err != nil {
			return reportError(streams.Out, global.compact, err)
		}
	}
	client, err := apiclient.New(global.apiURL, token, global.timeout, global.allowInsecureHTTP, nil)
	if err != nil {
		return reportError(streams.Out, global.compact, err)
	}
	executor := runner{client: client, streams: streams, compact: global.compact}
	if command[0] == "shell" {
		if err := executor.shell(ctx); err != nil {
			return reportError(streams.Out, global.compact, err)
		}
		return nil
	}
	result, err := executor.execute(ctx, command)
	if err != nil {
		return reportError(streams.Out, global.compact, err)
	}
	return writeJSON(streams.Out, result, global.compact)
}

func parseGlobal(args []string, getenv func(string) string) (options, []string, error) {
	value := options{apiURL: strings.TrimSpace(getenv("UA_API_URL")), tokenFile: strings.TrimSpace(getenv("UA_API_TOKEN_FILE")), timeout: 30 * time.Second}
	if value.apiURL == "" {
		value.apiURL = "http://127.0.0.1:8080"
	}
	flags := flag.NewFlagSet("cli", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&value.apiURL, "api-url", value.apiURL, "Upload Assistant API base URL")
	flags.StringVar(&value.tokenFile, "token-file", value.tokenFile, "file containing the bearer token")
	flags.DurationVar(&value.timeout, "timeout", value.timeout, "HTTP request timeout")
	flags.BoolVar(&value.compact, "compact", false, "write compact JSON")
	flags.BoolVar(&value.allowInsecureHTTP, "allow-insecure-http", false, "allow bearer authentication over non-loopback HTTP")
	if err := flags.Parse(args); err != nil {
		return value, nil, fmt.Errorf("invalid CLI options: %w", err)
	}
	return value, flags.Args(), nil
}

func normalizedStreams(streams Streams) Streams {
	if streams.In == nil {
		streams.In = os.Stdin
	}
	if streams.Out == nil {
		streams.Out = os.Stdout
	}
	if streams.Err == nil {
		streams.Err = os.Stderr
	}
	if streams.Getenv == nil {
		streams.Getenv = os.Getenv
	}
	return streams
}

func loadToken(tokenFile string, streams Streams) (string, error) {
	if token := strings.TrimSpace(streams.Getenv("UA_API_TOKEN")); token != "" {
		return validateToken(token)
	}
	if tokenFile != "" {
		file, err := os.Open(tokenFile)
		if err != nil {
			return "", fmt.Errorf("open API token file: %w", err)
		}
		defer file.Close()
		body, err := io.ReadAll(io.LimitReader(file, 4097))
		if err != nil {
			return "", fmt.Errorf("read API token file: %w", err)
		}
		if len(body) > 4096 {
			return "", errors.New("API token file exceeds 4096 bytes")
		}
		return validateToken(string(body))
	}
	if streams.ReadSecret != nil {
		token, err := streams.ReadSecret("API token: ")
		if err != nil {
			return "", fmt.Errorf("read API token: %w", err)
		}
		return validateToken(token)
	}
	return "", errors.New("API token is required through UA_API_TOKEN, --token-file, UA_API_TOKEN_FILE, or an interactive terminal")
}

func validateToken(token string) (string, error) {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, "ua_") || len(token) < 32 || strings.ContainsAny(token, "\r\n\x00") {
		return "", errors.New("API token format is invalid")
	}
	for _, character := range strings.TrimPrefix(token, "ua_") {
		if (character < 'a' || character > 'z') && (character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') && character != '-' && character != '_' {
			return "", errors.New("API token format is invalid")
		}
	}
	return token, nil
}

func (r runner) execute(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 {
		return nil, errors.New("a CLI command is required")
	}
	switch args[0] {
	case "health":
		return r.request(ctx, http.MethodGet, "/health/ready", nil, nil, nil, false)
	case "tools":
		return r.request(ctx, http.MethodGet, "/api/v2/tools", nil, nil, nil, true)
	case "jobs":
		return r.jobs(ctx, args[1:])
	case "retorrent":
		return r.retorrent(ctx, args[1:])
	case "candidates":
		return r.candidates(ctx, args[1:])
	case "sites":
		if len(args) != 1 {
			return nil, errors.New("usage: sites")
		}
		return r.request(ctx, http.MethodGet, "/api/v2/sites", nil, nil, nil, true)
	case "rules":
		return r.rules(ctx, args[1:])
	case "integrations":
		return r.integrations(ctx, args[1:])
	case "notifications":
		return r.notifications(ctx, args[1:])
	case "audit":
		return r.audit(ctx, args[1:])
	case "readiness":
		return r.readiness(ctx, args[1:])
	default:
		return nil, fmt.Errorf("unknown CLI command %q", args[0])
	}
}

func (r runner) request(ctx context.Context, method, requestPath string, query url.Values, body any, headers map[string]string, authenticated bool) (json.RawMessage, error) {
	return r.client.DoJSON(ctx, method, requestPath, query, body, headers, authenticated)
}

func (r runner) jobs(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 {
		return nil, errors.New("usage: jobs list|get|summary|steps|attempts|events|artifacts|pause|resume|retry|replay|cancel")
	}
	switch args[0] {
	case "list":
		flags := newFlags("jobs list")
		status := flags.String("status", "", "job status")
		kind := flags.String("kind", "", "job kind")
		limit := flags.Int("limit", 25, "result limit")
		cursor := flags.String("cursor", "", "pagination cursor")
		if err := parseFlags(flags, args[1:]); err != nil {
			return nil, err
		}
		query := url.Values{"limit": []string{strconv.Itoa(*limit)}}
		setQuery(query, "status", *status)
		setQuery(query, "kind", *kind)
		setQuery(query, "cursor", *cursor)
		return r.request(ctx, http.MethodGet, "/api/v2/jobs", query, nil, nil, true)
	case "get", "summary", "steps", "artifacts":
		if len(args) != 2 {
			return nil, fmt.Errorf("usage: jobs %s <job-id>", args[0])
		}
		id, err := validUUID(args[1], "job ID")
		if err != nil {
			return nil, err
		}
		suffix := ""
		if args[0] != "get" {
			suffix = "/" + args[0]
		}
		return r.request(ctx, http.MethodGet, "/api/v2/jobs/"+id+suffix, nil, nil, nil, true)
	case "events":
		flags := newFlags("jobs events")
		after := flags.Int64("after", 0, "event cursor")
		limit := flags.Int("limit", 100, "result limit")
		if len(args) < 2 {
			return nil, errors.New("usage: jobs events <job-id> [--after N] [--limit N]")
		}
		id, err := validUUID(args[1], "job ID")
		if err != nil {
			return nil, err
		}
		if err := parseFlags(flags, args[2:]); err != nil {
			return nil, err
		}
		query := url.Values{"after": []string{strconv.FormatInt(*after, 10)}, "limit": []string{strconv.Itoa(*limit)}}
		return r.request(ctx, http.MethodGet, "/api/v2/jobs/"+id+"/events", query, nil, nil, true)
	case "attempts":
		flags := newFlags("jobs attempts")
		limit := flags.Int("limit", 100, "result limit")
		cursor := flags.String("cursor", "", "opaque pagination cursor")
		if len(args) < 2 {
			return nil, errors.New("usage: jobs attempts <job-id> [--limit N] [--cursor CURSOR]")
		}
		id, err := validUUID(args[1], "job ID")
		if err != nil {
			return nil, err
		}
		if err := parseFlags(flags, args[2:]); err != nil {
			return nil, err
		}
		query := url.Values{"limit": []string{strconv.Itoa(*limit)}}
		setQuery(query, "cursor", *cursor)
		return r.request(ctx, http.MethodGet, "/api/v2/jobs/"+id+"/attempts", query, nil, nil, true)
	case "replay":
		if len(args) < 2 {
			return nil, errors.New("usage: jobs replay <job-id> [--execution-mode step|auto] [--stop-after-step STEP] [--idempotency-key KEY]")
		}
		id, err := validUUID(args[1], "job ID")
		if err != nil {
			return nil, err
		}
		flags := newFlags("jobs replay")
		mode := flags.String("execution-mode", "step", "fresh replay execution mode")
		stopAfter := flags.String("stop-after-step", "", "fresh replay workflow boundary")
		key := flags.String("idempotency-key", "", "stable replay intent key")
		if err := parseFlags(flags, args[2:]); err != nil {
			return nil, err
		}
		if strings.TrimSpace(*key) == "" {
			*key = "cli-replay-" + uuid.NewString()
		}
		body := map[string]any{"execution_mode": strings.TrimSpace(*mode)}
		if strings.TrimSpace(*stopAfter) != "" {
			body["stop_after_step"] = strings.TrimSpace(*stopAfter)
		}
		return r.request(ctx, http.MethodPost, "/api/v2/jobs/"+id+"/replay", nil, body, map[string]string{
			"Idempotency-Key": strings.TrimSpace(*key),
		}, true)
	case "pause", "cancel", "retry":
		if len(args) != 2 {
			return nil, fmt.Errorf("usage: jobs %s <job-id>", args[0])
		}
		id, err := validUUID(args[1], "job ID")
		if err != nil {
			return nil, err
		}
		action := args[0]
		body := any(nil)
		if action == "retry" {
			action = "resume"
			body = map[string]any{"resume_state": map[string]any{}}
		}
		return r.request(ctx, http.MethodPost, "/api/v2/jobs/"+id+"/"+action, nil, body, nil, true)
	case "resume":
		return r.resume(ctx, args[1:])
	default:
		return nil, fmt.Errorf("unknown jobs command %q", args[0])
	}
}

func (r runner) resume(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 {
		return nil, errors.New("usage: jobs resume <job-id> [--state JSON|--state-file FILE] [--accept-rule SITE=FINGERPRINT] [--obligation SITE:ID=EVIDENCE] [--confirm-upload]")
	}
	id, err := validUUID(args[0], "job ID")
	if err != nil {
		return nil, err
	}
	flags := newFlags("jobs resume")
	stateJSON := flags.String("state", "", "resume_state JSON object")
	stateFile := flags.String("state-file", "", "file containing resume_state JSON")
	confirmUpload := flags.Bool("confirm-upload", false, "explicitly confirm live upload")
	var acceptRules, obligations stringList
	flags.Var(&acceptRules, "accept-rule", "accepted rule SITE=FINGERPRINT (repeatable)")
	flags.Var(&obligations, "obligation", "manual evidence SITE:ID=EVIDENCE (repeatable)")
	if err := parseFlags(flags, args[1:]); err != nil {
		return nil, err
	}
	if *stateJSON != "" && *stateFile != "" {
		return nil, errors.New("--state and --state-file are mutually exclusive")
	}
	state := map[string]any{}
	if *stateJSON != "" {
		if err := json.Unmarshal([]byte(*stateJSON), &state); err != nil {
			return nil, fmt.Errorf("invalid --state JSON: %w", err)
		}
	}
	if *stateFile != "" {
		body, err := readBoundedFile(*stateFile, 256<<10)
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(body, &state); err != nil {
			return nil, fmt.Errorf("invalid resume state file: %w", err)
		}
	}
	acceptance, err := parseAcceptances(acceptRules, obligations)
	if err != nil {
		return nil, err
	}
	if len(acceptance) > 0 {
		state["accept_rules"] = acceptance
	}
	if *confirmUpload {
		if len(acceptance) < 2 {
			return nil, errors.New("--confirm-upload requires explicit --accept-rule values for both the source and target sites")
		}
		state["confirm_upload"] = true
	}
	return r.request(ctx, http.MethodPost, "/api/v2/jobs/"+id+"/resume", nil, map[string]any{"resume_state": state}, nil, true)
}

type retorrentOptions struct {
	sourceURL, target, executionMode, stopAfter, idempotencyKey        string
	downloader, savePath, category, tags                               string
	targetDownloader, targetSavePath, targetCategory, targetTags       string
	screenshotProfile, imageHost, tmdbProvider, ptgenProvider          string
	downloadLimit, uploadLimit, targetDownloadLimit, targetUploadLimit int64
	confirmUpload                                                      bool
	acceptRules, obligations                                           stringList
}

func (r runner) retorrent(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 || args[0] != "create" {
		return nil, errors.New("usage: retorrent create --source-url URL --target SITE [options]")
	}
	flags := newFlags("retorrent create")
	var value retorrentOptions
	bindRetorrentFlags(flags, &value)
	if err := parseFlags(flags, args[1:]); err != nil {
		return nil, err
	}
	value.sourceURL = strings.TrimSpace(value.sourceURL)
	value.target = strings.ToUpper(strings.TrimSpace(value.target))
	if value.sourceURL == "" || value.target == "" {
		return nil, errors.New("--source-url and --target are required")
	}
	if _, err := validSite(value.target); err != nil {
		return nil, err
	}
	acceptance, err := parseAcceptances(value.acceptRules, value.obligations)
	if err != nil {
		return nil, err
	}
	if value.confirmUpload {
		if _, exists := acceptance[value.target]; !exists || len(acceptance) < 2 {
			return nil, errors.New("--confirm-upload requires explicit --accept-rule values for both the source and target sites")
		}
	}
	input := map[string]any{"source_url": value.sourceURL, "target": value.target, "confirm_upload": value.confirmUpload}
	if len(acceptance) > 0 {
		input["accept_rules"] = acceptance
	}
	if control := downloaderInput(value.downloader, value.savePath, value.category, value.tags, value.downloadLimit, value.uploadLimit); control != nil {
		input["downloader"] = control
	}
	if control := downloaderInput(value.targetDownloader, value.targetSavePath, value.targetCategory, value.targetTags, value.targetDownloadLimit, value.targetUploadLimit); control != nil {
		input["target_downloader"] = control
	}
	if value.screenshotProfile != "" {
		input["screenshots"] = map[string]any{"profile": strings.TrimSpace(value.screenshotProfile)}
	}
	if value.imageHost != "" {
		input["image_host"] = map[string]any{"name": strings.TrimSpace(value.imageHost)}
	}
	if value.tmdbProvider != "" || value.ptgenProvider != "" {
		providers := map[string]any{}
		if name := strings.TrimSpace(value.tmdbProvider); name != "" {
			providers["tmdb"] = name
		}
		if name := strings.TrimSpace(value.ptgenProvider); name != "" {
			providers["ptgen"] = name
		}
		input["metadata_providers"] = providers
	}
	mode := strings.TrimSpace(value.executionMode)
	if mode == "" {
		mode = "step"
	}
	if mode != "step" && mode != "auto" {
		return nil, errors.New("--execution-mode must be step or auto")
	}
	body := map[string]any{"kind": "retorrent", "execution_mode": mode, "input": input}
	if value.stopAfter != "" {
		body["stop_after_step"] = strings.TrimSpace(value.stopAfter)
	}
	key := strings.TrimSpace(value.idempotencyKey)
	if key == "" {
		key = "cli-" + uuid.NewString()
	}
	return r.request(ctx, http.MethodPost, "/api/v2/jobs", nil, body, map[string]string{"Idempotency-Key": key}, true)
}

func bindRetorrentFlags(flags *flag.FlagSet, value *retorrentOptions) {
	flags.StringVar(&value.sourceURL, "source-url", "", "source tracker details URL")
	flags.StringVar(&value.target, "target", "", "target site code")
	flags.StringVar(&value.executionMode, "execution-mode", "step", "step or auto")
	flags.StringVar(&value.stopAfter, "stop-after-step", "", "pause after this workflow step")
	flags.StringVar(&value.idempotencyKey, "idempotency-key", "", "stable retry key")
	flags.StringVar(&value.downloader, "downloader", "", "source downloader name")
	flags.StringVar(&value.savePath, "save-path", "", "source downloader save path")
	flags.StringVar(&value.category, "category", "", "source downloader category")
	flags.StringVar(&value.tags, "tags", "", "comma-delimited source downloader tags")
	flags.Int64Var(&value.downloadLimit, "download-limit", 0, "source download limit in bytes/second")
	flags.Int64Var(&value.uploadLimit, "upload-limit", 0, "source upload limit in bytes/second")
	flags.StringVar(&value.targetDownloader, "target-downloader", "", "target seeding downloader name")
	flags.StringVar(&value.targetSavePath, "target-save-path", "", "target downloader save path")
	flags.StringVar(&value.targetCategory, "target-category", "", "target downloader category")
	flags.StringVar(&value.targetTags, "target-tags", "", "comma-delimited target downloader tags")
	flags.Int64Var(&value.targetDownloadLimit, "target-download-limit", 0, "target download limit in bytes/second")
	flags.Int64Var(&value.targetUploadLimit, "target-upload-limit", 0, "target upload limit in bytes/second")
	flags.StringVar(&value.screenshotProfile, "screenshot-profile", "", "configured screenshot profile")
	flags.StringVar(&value.imageHost, "image-host", "", "configured image host")
	flags.StringVar(&value.tmdbProvider, "tmdb-provider", "", "configured TMDb metadata provider")
	flags.StringVar(&value.ptgenProvider, "ptgen-provider", "", "configured PTGen metadata provider")
	flags.Var(&value.acceptRules, "accept-rule", "accepted rule SITE=FINGERPRINT (repeatable)")
	flags.Var(&value.obligations, "obligation", "manual evidence SITE:ID=EVIDENCE (repeatable)")
	flags.BoolVar(&value.confirmUpload, "confirm-upload", false, "explicitly confirm live upload")
}

func downloaderInput(name, savePath, category, tags string, downloadLimit, uploadLimit int64) map[string]any {
	name = strings.TrimSpace(name)
	if name == "" {
		return nil
	}
	result := map[string]any{"name": name, "save_path": strings.TrimSpace(savePath)}
	if category != "" {
		result["category"] = strings.TrimSpace(category)
	}
	if values := commaValues(tags); len(values) > 0 {
		result["tags"] = values
	}
	if downloadLimit > 0 {
		result["download_limit_bytes_per_second"] = downloadLimit
	}
	if uploadLimit > 0 {
		result["upload_limit_bytes_per_second"] = uploadLimit
	}
	return result
}

func (r runner) candidates(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 {
		return nil, errors.New("usage: candidates list|scan|submit")
	}
	switch args[0] {
	case "list":
		flags := newFlags("candidates list")
		source := flags.String("source", "", "source site")
		target := flags.String("target", "", "target site")
		date := flags.String("date", "", "recommendation date YYYY-MM-DD")
		status := flags.String("status", "", "candidate status")
		limit := flags.Int("limit", 10, "result limit")
		if err := parseFlags(flags, args[1:]); err != nil {
			return nil, err
		}
		query := url.Values{"limit": []string{strconv.Itoa(*limit)}}
		setQuery(query, "source", strings.ToUpper(strings.TrimSpace(*source)))
		setQuery(query, "target", strings.ToUpper(strings.TrimSpace(*target)))
		setQuery(query, "date", *date)
		setQuery(query, "status", *status)
		return r.request(ctx, http.MethodGet, "/api/v2/candidates/daily", query, nil, nil, true)
	case "scan":
		flags := newFlags("candidates scan")
		source := flags.String("source", "", "source site")
		target := flags.String("target", "", "target site")
		targetCount := flags.Int("target-count", 10, "desired candidate count")
		scanLimit := flags.Int("scan-limit", 30, "source scan limit")
		page := flags.Int("page", 0, "source page")
		date := flags.String("date", "", "recommendation date YYYY-MM-DD")
		mode := flags.String("execution-mode", "auto", "step or auto")
		stopAfter := flags.String("stop-after-step", "", "pause after this workflow step")
		key := flags.String("idempotency-key", "", "stable retry key")
		if err := parseFlags(flags, args[1:]); err != nil {
			return nil, err
		}
		if *source == "" || *target == "" {
			return nil, errors.New("--source and --target are required")
		}
		body := map[string]any{"source": strings.ToUpper(strings.TrimSpace(*source)), "target": strings.ToUpper(strings.TrimSpace(*target)), "target_count": *targetCount, "scan_limit": *scanLimit, "page": *page, "date": strings.TrimSpace(*date), "execution_mode": strings.TrimSpace(*mode)}
		if *stopAfter != "" {
			body["stop_after_step"] = strings.TrimSpace(*stopAfter)
		}
		if strings.TrimSpace(*key) == "" {
			*key = "cli-candidates-" + uuid.NewString()
		}
		return r.request(ctx, http.MethodPost, "/api/v2/candidates/daily", nil, body, map[string]string{"Idempotency-Key": strings.TrimSpace(*key)}, true)
	case "submit":
		if len(args) < 2 {
			return nil, errors.New("usage: candidates submit <candidate-id> [--execution-mode step|auto] [--idempotency-key KEY]")
		}
		id, err := validUUID(args[1], "candidate ID")
		if err != nil {
			return nil, err
		}
		flags := newFlags("candidates submit")
		mode := flags.String("execution-mode", "step", "step or auto")
		stopAfter := flags.String("stop-after-step", "", "pause after this workflow step")
		key := flags.String("idempotency-key", "", "stable retry key")
		if err := parseFlags(flags, args[2:]); err != nil {
			return nil, err
		}
		body := map[string]any{"execution_mode": strings.TrimSpace(*mode)}
		if *stopAfter != "" {
			body["stop_after_step"] = strings.TrimSpace(*stopAfter)
		}
		if strings.TrimSpace(*key) == "" {
			*key = "cli-submit-" + uuid.NewString()
		}
		return r.request(ctx, http.MethodPost, "/api/v2/candidates/"+id+"/retorrent-job", nil, body, map[string]string{"Idempotency-Key": strings.TrimSpace(*key)}, true)
	default:
		return nil, fmt.Errorf("unknown candidates command %q", args[0])
	}
}

func (r runner) rules(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) != 2 || args[0] != "active" {
		return nil, errors.New("usage: rules active <site-code>")
	}
	site, err := validSite(args[1])
	if err != nil {
		return nil, err
	}
	return r.request(ctx, http.MethodGet, "/api/v2/sites/"+site+"/rules/active", nil, nil, nil, true)
}

func (r runner) integrations(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) != 2 || args[0] != "list" {
		return nil, errors.New("usage: integrations list downloaders|image-hosts|screenshot-profiles|notification-channels|media-managers|metadata-providers")
	}
	allowed := map[string]string{
		"downloaders": "/api/v2/downloaders", "image-hosts": "/api/v2/image-hosts",
		"screenshot-profiles": "/api/v2/screenshot-profiles", "notification-channels": "/api/v2/notification-channels",
		"media-managers":     "/api/v2/media-managers",
		"metadata-providers": "/api/v2/metadata-providers",
	}
	requestPath, exists := allowed[args[1]]
	if !exists {
		return nil, fmt.Errorf("unsupported integration collection %q", args[1])
	}
	return r.request(ctx, http.MethodGet, requestPath, nil, nil, nil, true)
}

func (r runner) notifications(ctx context.Context, args []string) (json.RawMessage, error) {
	flags := newFlags("notifications")
	limit := flags.Int("limit", 25, "result limit")
	if err := parseFlags(flags, args); err != nil {
		return nil, err
	}
	return r.request(ctx, http.MethodGet, "/api/v2/notifications", url.Values{"limit": []string{strconv.Itoa(*limit)}}, nil, nil, true)
}

func (r runner) audit(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 || args[0] != "list" {
		return nil, errors.New("usage: audit list [--actor-type TYPE] [--action ACTION] [--resource-type TYPE] [--resource-id ID] [--limit N] [--cursor CURSOR]")
	}
	flags := newFlags("audit list")
	actorType := flags.String("actor-type", "", "exact actor type")
	action := flags.String("action", "", "exact audited action")
	resourceType := flags.String("resource-type", "", "exact resource type")
	resourceID := flags.String("resource-id", "", "exact resource identifier")
	limit := flags.Int("limit", 50, "result limit")
	cursor := flags.String("cursor", "", "pagination cursor")
	if err := parseFlags(flags, args[1:]); err != nil {
		return nil, err
	}
	query := url.Values{"limit": []string{strconv.Itoa(*limit)}}
	setQuery(query, "actor_type", *actorType)
	setQuery(query, "action", *action)
	setQuery(query, "resource_type", *resourceType)
	setQuery(query, "resource_id", *resourceID)
	setQuery(query, "cursor", *cursor)
	return r.request(ctx, http.MethodGet, "/api/v2/audit-events", query, nil, nil, true)
}

func (r runner) readiness(ctx context.Context, args []string) (json.RawMessage, error) {
	if len(args) == 0 || args[0] != "live" {
		return nil, errors.New("usage: readiness live --source U2|CHD --target MTEAM --downloader NAME [--target-downloader NAME] --image-host NAME --screenshot-profile NAME --tmdb-provider NAME --ptgen-provider NAME")
	}
	flags := newFlags("readiness live")
	source := flags.String("source", "", "source site code")
	target := flags.String("target", "", "target site code")
	downloader := flags.String("downloader", "", "source downloader name")
	targetDownloader := flags.String("target-downloader", "", "target downloader name; defaults to source downloader")
	imageHost := flags.String("image-host", "", "image host name")
	screenshotProfile := flags.String("screenshot-profile", "", "screenshot profile name")
	tmdbProvider := flags.String("tmdb-provider", "", "TMDb metadata provider name")
	ptgenProvider := flags.String("ptgen-provider", "", "PTGen metadata provider name")
	if err := parseFlags(flags, args[1:]); err != nil {
		return nil, err
	}
	if strings.TrimSpace(*source) == "" || strings.TrimSpace(*target) == "" || strings.TrimSpace(*downloader) == "" || strings.TrimSpace(*imageHost) == "" || strings.TrimSpace(*screenshotProfile) == "" || strings.TrimSpace(*tmdbProvider) == "" || strings.TrimSpace(*ptgenProvider) == "" {
		return nil, errors.New("source, target, downloader, image-host, screenshot-profile, tmdb-provider, and ptgen-provider are required")
	}
	query := url.Values{
		"source":             []string{strings.ToUpper(strings.TrimSpace(*source))},
		"target":             []string{strings.ToUpper(strings.TrimSpace(*target))},
		"downloader":         []string{strings.TrimSpace(*downloader)},
		"image_host":         []string{strings.TrimSpace(*imageHost)},
		"screenshot_profile": []string{strings.TrimSpace(*screenshotProfile)},
		"tmdb_provider":      []string{strings.TrimSpace(*tmdbProvider)},
		"ptgen_provider":     []string{strings.TrimSpace(*ptgenProvider)},
	}
	setQuery(query, "target_downloader", *targetDownloader)
	return r.request(ctx, http.MethodGet, "/api/v2/readiness/live", query, nil, nil, true)
}

func (r runner) shell(ctx context.Context) error {
	_, _ = io.WriteString(r.streams.Err, "Upload Assistant 交互 CLI。输入 help 查看命令，exit 退出。\n")
	scanner := bufio.NewScanner(io.LimitReader(r.streams.In, 4<<20))
	scanner.Buffer(make([]byte, 4096), 64<<10)
	for {
		_, _ = io.WriteString(r.streams.Err, "ua> ")
		if !scanner.Scan() {
			break
		}
		args, err := splitCommandLine(scanner.Text())
		if err != nil {
			_ = writeFailure(r.streams.Out, r.compact, err)
			continue
		}
		if len(args) == 0 {
			continue
		}
		if args[0] == "exit" || args[0] == "quit" {
			return nil
		}
		if args[0] == "help" {
			_, _ = io.WriteString(r.streams.Err, usage())
			continue
		}
		result, err := r.execute(ctx, args)
		if err != nil {
			_ = writeFailure(r.streams.Out, r.compact, err)
			continue
		}
		if err := writeJSON(r.streams.Out, result, r.compact); err != nil {
			return err
		}
	}
	return scanner.Err()
}

type acceptance struct {
	Fingerprint string                    `json:"fingerprint"`
	Accepted    bool                      `json:"accepted"`
	Obligations map[string]map[string]any `json:"obligations,omitempty"`
}

func parseAcceptances(ruleValues, obligationValues []string) (map[string]acceptance, error) {
	result := map[string]acceptance{}
	for _, value := range ruleValues {
		siteValue, fingerprint, found := strings.Cut(value, "=")
		site, err := validSite(siteValue)
		if err != nil || !found || strings.TrimSpace(fingerprint) == "" {
			return nil, fmt.Errorf("invalid --accept-rule %q; expected SITE=FINGERPRINT", value)
		}
		fingerprint = strings.TrimSpace(fingerprint)
		if len(fingerprint) != 64 || !isLowerHex(strings.ToLower(fingerprint)) {
			return nil, fmt.Errorf("invalid --accept-rule fingerprint for %s; expected 64 hexadecimal characters", site)
		}
		result[site] = acceptance{Fingerprint: strings.ToLower(fingerprint), Accepted: true, Obligations: map[string]map[string]any{}}
	}
	for _, value := range obligationValues {
		binding, evidence, found := strings.Cut(value, "=")
		siteValue, obligationID, hasID := strings.Cut(binding, ":")
		site, err := validSite(siteValue)
		if err != nil || !found || !hasID || strings.TrimSpace(obligationID) == "" || strings.TrimSpace(evidence) == "" {
			return nil, fmt.Errorf("invalid --obligation %q; expected SITE:ID=EVIDENCE", value)
		}
		accepted, exists := result[site]
		if !exists {
			return nil, fmt.Errorf("--obligation for %s requires a matching --accept-rule", site)
		}
		accepted.Obligations[strings.TrimSpace(obligationID)] = map[string]any{"confirmed": true, "evidence": strings.TrimSpace(evidence)}
		result[site] = accepted
	}
	return result, nil
}

type stringList []string

func (values *stringList) String() string { return strings.Join(*values, ",") }
func (values *stringList) Set(value string) error {
	*values = append(*values, value)
	return nil
}

func newFlags(name string) *flag.FlagSet {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	return flags
}

func parseFlags(flags *flag.FlagSet, args []string) error {
	if err := flags.Parse(args); err != nil {
		return fmt.Errorf("invalid %s options: %w", flags.Name(), err)
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected %s arguments: %s", flags.Name(), strings.Join(flags.Args(), " "))
	}
	return nil
}

func setQuery(query url.Values, name, value string) {
	if value = strings.TrimSpace(value); value != "" {
		query.Set(name, value)
	}
}

func validUUID(value, label string) (string, error) {
	id, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil {
		return "", fmt.Errorf("%s must be a UUID", label)
	}
	return id.String(), nil
}

func validSite(value string) (string, error) {
	value = strings.ToUpper(strings.TrimSpace(value))
	if value == "" || len(value) > 20 {
		return "", errors.New("site code is invalid")
	}
	for _, character := range value {
		if (character < 'A' || character > 'Z') && (character < '0' || character > '9') && character != '_' {
			return "", errors.New("site code is invalid")
		}
	}
	return value, nil
}

func commaValues(value string) []string {
	seen := map[string]bool{}
	result := []string{}
	for _, item := range strings.Split(value, ",") {
		item = strings.TrimSpace(item)
		if item != "" && !seen[item] {
			seen[item] = true
			result = append(result, item)
		}
	}
	return result
}

func isLowerHex(value string) bool {
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func readBoundedFile(filename string, limit int64) ([]byte, error) {
	file, err := os.Open(filename)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", filename, err)
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", filename, err)
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("%s exceeds %d bytes", filename, limit)
	}
	return body, nil
}

func splitCommandLine(line string) ([]string, error) {
	var result []string
	var current strings.Builder
	quote := rune(0)
	escaped := false
	flush := func() {
		if current.Len() > 0 {
			result = append(result, current.String())
			current.Reset()
		}
	}
	for _, character := range line {
		if escaped {
			current.WriteRune(character)
			escaped = false
			continue
		}
		if quote != '\'' && character == '\\' {
			escaped = true
			continue
		}
		if quote != 0 {
			if character == quote {
				quote = 0
			} else {
				current.WriteRune(character)
			}
			continue
		}
		if character == '\'' || character == '"' {
			quote = character
			continue
		}
		if unicode.IsSpace(character) {
			flush()
			continue
		}
		current.WriteRune(character)
	}
	if escaped || quote != 0 {
		return nil, errors.New("interactive command contains an unfinished quote or escape")
	}
	flush()
	return result, nil
}

func writeJSON(output io.Writer, body json.RawMessage, compact bool) error {
	if !json.Valid(body) {
		return errors.New("cannot print invalid JSON")
	}
	if compact {
		_, err := fmt.Fprintf(output, "%s\n", body)
		return err
	}
	var formatted bytes.Buffer
	if err := json.Indent(&formatted, body, "", "  "); err != nil {
		return err
	}
	_, err := fmt.Fprintln(output, formatted.String())
	return err
}

func reportError(output io.Writer, compact bool, err error) error {
	if writeErr := writeFailure(output, compact, err); writeErr != nil {
		return writeErr
	}
	return ErrReported
}

func writeFailure(output io.Writer, compact bool, err error) error {
	code := "cli_error"
	message := err.Error()
	var apiError *apiclient.Error
	if errors.As(err, &apiError) && apiError.Code != "" {
		code = apiError.Code
		message = apiError.Detail
	}
	body, _ := json.Marshal(map[string]any{
		"ok": false, "status": "failed",
		"blockers":     []map[string]string{{"code": code, "message": message}},
		"next_actions": []map[string]string{{"action": "review_cli_request_and_service_status"}},
	})
	return writeJSON(output, body, compact)
}

func usage() string {
	return `Upload-Assistant v2 API CLI

Usage:
  upload-assistant cli [global options] health
  upload-assistant cli [global options] tools
  upload-assistant cli [global options] jobs list|get|summary|steps|attempts|events|artifacts|pause|resume|retry|replay|cancel ...
  upload-assistant cli [global options] retorrent create --source-url URL --target SITE [options]
  upload-assistant cli [global options] candidates list|scan|submit ...
  upload-assistant cli [global options] sites
  upload-assistant cli [global options] rules active SITE
  upload-assistant cli [global options] integrations list COLLECTION
  upload-assistant cli [global options] notifications [--limit N]
  upload-assistant cli [global options] audit list [filters]
  upload-assistant cli [global options] readiness live --source U2|CHD --target MTEAM --downloader NAME --image-host NAME --screenshot-profile NAME --tmdb-provider NAME --ptgen-provider NAME
  upload-assistant cli [global options] shell

Global options:
  --api-url URL               default UA_API_URL or http://127.0.0.1:8080
  --token-file FILE           bearer token file; alternatively UA_API_TOKEN_FILE or UA_API_TOKEN
  --timeout DURATION          request timeout, default 30s
  --compact                   compact JSON output
  --allow-insecure-http       explicitly allow non-loopback plaintext HTTP

Safety:
  Live upload confirmation is never inferred. Use exact repeated
  --accept-rule SITE=FINGERPRINT values and --confirm-upload. Supply manual
  obligations as --obligation SITE:ID=EVIDENCE. The service revalidates every
  rule, duplicate, confirmation, and seeding gate. The readiness command only
  checks local configuration and never calls an external service or authorizes upload.
`
}
