package deployment

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestDefaultComposeIsCanonicalGoV2Deployment(t *testing.T) {
	root := repositoryRoot(t)
	canonical := readFile(t, filepath.Join(root, "docker-compose.yml"))
	compatibility := readFile(t, filepath.Join(root, "docker-compose.go.yml"))
	if !bytes.Equal(canonical, compatibility) {
		t.Fatal("docker-compose.go.yml must remain an exact compatibility copy of canonical docker-compose.yml")
	}
	var document struct {
		Services map[string]map[string]any `yaml:"services"`
	}
	if err := yaml.Unmarshal(canonical, &document); err != nil {
		t.Fatalf("parse canonical Compose: %v", err)
	}
	for _, service := range []string{"postgres", "upload-assistant"} {
		if document.Services[service] == nil {
			t.Errorf("canonical Compose is missing %s", service)
		}
	}
	text := string(canonical)
	for _, required := range []string{
		"Dockerfile.v2", "platform: linux/amd64", "127.0.0.1:${UA_HTTP_PORT:-8080}:8080",
		"${UA_POSTGRES_PASSWORD:?", "read_only: true", "no-new-privileges:true", "cap_drop:",
		"/legacy:ro", "/downloads:rw", "UA_MASTER_KEY_FILE: /data/master-keys",
	} {
		if !strings.Contains(text, required) {
			t.Errorf("canonical Compose is missing %q", required)
		}
	}
	for _, forbidden := range []string{"Dockerfile.ptcli", "ptcli-api", "python ", "0.0.0.0:${UA_HTTP_PORT"} {
		if strings.Contains(strings.ToLower(text), strings.ToLower(forbidden)) {
			t.Errorf("canonical Compose still contains legacy or unsafe deployment text %q", forbidden)
		}
	}
}

func TestPublishedImagesUseOnlyGoV2AMD64Runtime(t *testing.T) {
	root := repositoryRoot(t)
	for _, name := range []string{"docker-build.yml", "docker-image.yml"} {
		path := filepath.Join(root, ".github", "workflows", name)
		body := readFile(t, path)
		var parsed yaml.Node
		if err := yaml.Unmarshal(body, &parsed); err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		text := string(body)
		if !strings.Contains(text, "Dockerfile.v2") || !strings.Contains(text, "linux/amd64") {
			t.Errorf("%s does not publish the Go v2 linux/amd64 image", name)
		}
		for _, forbidden := range []string{"Dockerfile.ptcli", "dockerfile: ./Dockerfile\n", "legacy-webui", "linux/arm64"} {
			if strings.Contains(text, forbidden) {
				t.Errorf("%s still publishes forbidden runtime %q", name, forbidden)
			}
		}
	}
	dockerfile := string(readFile(t, filepath.Join(root, "Dockerfile.v2")))
	if strings.Contains(strings.ToLower(dockerfile), "python") || !strings.Contains(dockerfile, "ENTRYPOINT [\"/usr/local/bin/upload-assistant\"]") {
		t.Fatal("Dockerfile.v2 must contain only the native Go service runtime")
	}
}

func TestGoV2CIAndPublicDocumentationAreTheDefault(t *testing.T) {
	root := repositoryRoot(t)
	workflowBytes := readFile(t, filepath.Join(root, ".github", "workflows", "go-v2.yml"))
	var workflowDocument yaml.Node
	if err := yaml.Unmarshal(workflowBytes, &workflowDocument); err != nil {
		t.Fatalf("parse Go v2 workflow: %v", err)
	}
	workflow := string(workflowBytes)
	for _, required := range []string{"make go-check", "go test -p 1 ./internal/... ./migrations", "go test -race ./internal/...", "verify_go_v2_local_ready.sh"} {
		if !strings.Contains(workflow, required) {
			t.Errorf("Go v2 workflow is missing %q", required)
		}
	}
	readme := string(readFile(t, filepath.Join(root, "README.md")))
	for _, forbidden := range []string{"/v1/", "ptcli serve", "docker compose up -d ptcli-api", "Dockerfile.ptcli"} {
		if strings.Contains(readme, forbidden) {
			t.Errorf("README still advertises the retired default %q", forbidden)
		}
	}
	environment := string(readFile(t, filepath.Join(root, ".env.example")))
	if !strings.Contains(environment, "UA_POSTGRES_PASSWORD=\n") || !strings.Contains(environment, "UA_DOWNLOADS_HOST_PATH=") {
		t.Fatal(".env.example must require an explicit database password and document the downloads mount")
	}
	makefile := string(readFile(t, filepath.Join(root, "Makefile")))
	for _, required := range []string{"lint: go-lint", "test: go-test", "check: go-check", "smoke: go-build"} {
		if !strings.Contains(makefile, required) {
			t.Errorf("default developer workflow is missing %q", required)
		}
	}
	for _, forbidden := range []string{"test: test-ptcli", "check: check-ptcli", "smoke: smoke-ptcli"} {
		if strings.Contains(makefile, forbidden) {
			t.Errorf("default developer workflow still routes to legacy target %q", forbidden)
		}
	}
}

func repositoryRoot(t *testing.T) string {
	t.Helper()
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve deployment contract source path")
	}
	return filepath.Clean(filepath.Join(filepath.Dir(filename), "..", ".."))
}

func readFile(t *testing.T, path string) []byte {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return body
}
