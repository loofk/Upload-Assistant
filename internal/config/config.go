package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultListenAddr     = ":8080"
	defaultDataDir        = "/data"
	defaultShutdown       = 15 * time.Second
	defaultDatabaseMaxCon = int32(10)
)

type Config struct {
	ListenAddr       string
	DatabaseURL      string
	DataDir          string
	LogLevel         string
	ShutdownTimeout  time.Duration
	DatabaseMaxConns int32
	DatabaseMinConns int32
	DatabaseMaxIdle  time.Duration
	DatabaseMaxLife  time.Duration
	MasterKeyFile    string
	MediaInfoBinary  string
	FFmpegBinary     string
	FFprobeBinary    string
	MkbrrBinary      string
}

type LookupEnv func(string) (string, bool)

func Load() (Config, error) {
	return LoadFrom(os.LookupEnv)
}

func LoadFrom(lookup LookupEnv) (Config, error) {
	cfg := Config{
		ListenAddr:       envOrDefault(lookup, "UA_LISTEN_ADDR", defaultListenAddr),
		DatabaseURL:      strings.TrimSpace(envOrDefault(lookup, "UA_DATABASE_URL", "")),
		DataDir:          filepath.Clean(envOrDefault(lookup, "UA_DATA_DIR", defaultDataDir)),
		LogLevel:         strings.ToLower(envOrDefault(lookup, "UA_LOG_LEVEL", "info")),
		ShutdownTimeout:  defaultShutdown,
		DatabaseMaxConns: defaultDatabaseMaxCon,
		DatabaseMinConns: 1,
		DatabaseMaxIdle:  5 * time.Minute,
		DatabaseMaxLife:  30 * time.Minute,
		MediaInfoBinary:  envOrDefault(lookup, "UA_MEDIAINFO_BIN", "mediainfo"),
		FFmpegBinary:     envOrDefault(lookup, "UA_FFMPEG_BIN", "ffmpeg"),
		FFprobeBinary:    envOrDefault(lookup, "UA_FFPROBE_BIN", "ffprobe"),
		MkbrrBinary:      envOrDefault(lookup, "UA_MKBRR_BIN", "mkbrr"),
	}
	cfg.MasterKeyFile = filepath.Clean(envOrDefault(lookup, "UA_MASTER_KEY_FILE", filepath.Join(cfg.DataDir, "master-keys")))
	if cfg.DatabaseURL == "" {
		return Config{}, errors.New("UA_DATABASE_URL is required")
	}
	if !filepath.IsAbs(cfg.DataDir) {
		return Config{}, fmt.Errorf("UA_DATA_DIR must be an absolute path: %s", cfg.DataDir)
	}
	if !filepath.IsAbs(cfg.MasterKeyFile) {
		return Config{}, fmt.Errorf("UA_MASTER_KEY_FILE must be an absolute path: %s", cfg.MasterKeyFile)
	}
	for _, binaryConfig := range []struct {
		variable string
		binary   string
	}{
		{variable: "UA_MEDIAINFO_BIN", binary: cfg.MediaInfoBinary},
		{variable: "UA_FFMPEG_BIN", binary: cfg.FFmpegBinary},
		{variable: "UA_FFPROBE_BIN", binary: cfg.FFprobeBinary},
		{variable: "UA_MKBRR_BIN", binary: cfg.MkbrrBinary},
	} {
		if strings.ContainsAny(binaryConfig.binary, "\r\n\x00") || strings.TrimSpace(binaryConfig.binary) == "" {
			return Config{}, fmt.Errorf("%s must be a binary name or absolute path", binaryConfig.variable)
		}
		if strings.ContainsAny(binaryConfig.binary, `/\\`) && !filepath.IsAbs(binaryConfig.binary) {
			return Config{}, fmt.Errorf("%s path must be absolute: %s", binaryConfig.variable, binaryConfig.binary)
		}
	}
	switch cfg.LogLevel {
	case "debug", "info", "warn", "error":
	default:
		return Config{}, fmt.Errorf("UA_LOG_LEVEL must be debug, info, warn, or error: %s", cfg.LogLevel)
	}
	return cfg, nil
}

func EnsureDataDirectories(dataDir string) error {
	for _, name := range []string{"artifacts", "rules", "tmp"} {
		path := filepath.Join(dataDir, name)
		if err := os.MkdirAll(path, 0o750); err != nil {
			return fmt.Errorf("create %s: %w", path, err)
		}
	}
	return nil
}

func envOrDefault(lookup LookupEnv, key, fallback string) string {
	if value, ok := lookup(key); ok && strings.TrimSpace(value) != "" {
		return strings.TrimSpace(value)
	}
	return fallback
}
