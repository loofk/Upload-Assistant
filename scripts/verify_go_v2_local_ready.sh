#!/usr/bin/env bash
set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root_dir/docker-compose.yml"
report_path="${1:-$root_dir/tmp/go-v2-local-ready.json}"

for command_name in docker curl jq openssl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "missing required command: $command_name" >&2
    exit 1
  fi
done

verify_root="$(mktemp -d /tmp/upload-assistant-v2-verify.XXXXXX)"
verify_suffix="$(openssl rand -hex 5)"
verify_project="ua-v2-verify-$verify_suffix"
mkdir -p "$verify_root/downloads" "$verify_root/legacy" "$(dirname "$report_path")"

export UA_POSTGRES_DB="upload_assistant"
export UA_POSTGRES_USER="upload_assistant"
export UA_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export UA_HTTP_PORT="0"
export UA_DOCKER_NETWORK="$verify_project-network"
export UA_DOWNLOADS_HOST_PATH="$verify_root/downloads"
export UA_LEGACY_DATA_HOST_PATH="$verify_root/legacy"

compose() {
  docker compose -p "$verify_project" -f "$compose_file" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  case "$verify_root" in
    /tmp/upload-assistant-v2-verify.*) rm -rf -- "$verify_root" ;;
  esac
}
trap cleanup EXIT INT TERM

wait_ready() {
  local attempt current_address
  for attempt in $(seq 1 120); do
    current_address="$(compose port upload-assistant 8080 2>/dev/null || true)"
    if [[ "$current_address" =~ ^127\.0\.0\.1:[0-9]+$ ]]; then
      published_address="$current_address"
      base_url="http://$published_address"
    fi
    if [[ -z "${base_url:-}" ]]; then
      sleep 1
      continue
    fi
    if curl --fail --silent --show-error "$base_url/health/ready" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "isolated Compose service did not become ready" >&2
  compose ps >&2 || true
  compose logs --no-color upload-assistant >&2 || true
  return 1
}

compose config --quiet
compose up -d --build
published_address=""
base_url=""
wait_ready

bootstrap_password="$(openssl rand -hex 24)"
bootstrap_json="$(printf '%s\n' "$bootstrap_password" | compose exec -T upload-assistant upload-assistant admin bootstrap --username verify-admin)"
unset bootstrap_password
api_token="$(jq -er '.admin.token' <<<"$bootstrap_json")"
if [[ "$api_token" != ua_* ]]; then
  echo "bootstrap did not return an API token" >&2
  exit 1
fi
auth_header="Authorization: Bearer $api_token"

health_json="$(curl --fail --silent --show-error "$base_url/health/ready")"
openapi_json="$(curl --fail --silent --show-error "$base_url/openapi.json")"
tools_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/tools")"
audit_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/audit-events?limit=5")"
readiness_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/readiness/live?source=U2&target=MTEAM&downloader=box&image_host=imgbb&screenshot_profile=default&tmdb_provider=tmdb-main&ptgen_provider=ptgen-main")"
cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact audit list --limit 2)"
readiness_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact readiness live --source U2 --target MTEAM --downloader box --image-host imgbb --screenshot-profile default --tmdb-provider tmdb-main --ptgen-provider ptgen-main)"

jq -e '.ok == true and .status == "ready" and .checks.database == "ready" and .checks.data_dir == "ready"' <<<"$health_json" >/dev/null
jq -e '.openapi == "3.1.0" and .paths["/api/v2/jobs"] and .paths["/api/v2/jobs/{job_id}/attempts"] and .paths["/api/v2/jobs/{job_id}/replay"] and .paths["/api/v2/audit-events"] and .paths["/api/v2/readiness/live"] and .components.schemas.RetorrentSummary and .components.schemas.ReplayJobRequest and .components.schemas.StepAttemptListEnvelope and .components.schemas.LiveReadinessReport' <<<"$openapi_json" >/dev/null
jq -e '.ok == true and .status == "ready" and .count >= 43 and any(.tools[]; .name == "list_audit_events") and any(.tools[]; .name == "get_job_attempts") and any(.tools[]; .name == "replay_job") and any(.tools[]; .name == "get_live_readiness") and any(.tools[]; .name == "resolve_external_metadata")' <<<"$tools_json" >/dev/null
jq -e '.ok == true and .status == "ready" and (.audit_events | type == "array")' <<<"$audit_json" >/dev/null
jq -e '.ok == true and .status == "ready" and (.audit_events | type == "array")' <<<"$cli_json" >/dev/null
jq -e '.status == "blocked" and .configuration_ready == false and .external_calls_performed == false and .live_upload_authorized == false and .resume_state.confirm_upload == false and (.blockers | length > 0)' <<<"$readiness_json" >/dev/null
jq -e '.status == "blocked" and .external_calls_performed == false and .live_upload_authorized == false and .resume_state.confirm_upload == false' <<<"$readiness_cli_json" >/dev/null

unauthorized_status="$(curl --silent --show-error -o "$verify_root/unauthorized.json" -w '%{http_code}' "$base_url/api/v2/jobs")"
if [[ "$unauthorized_status" != "401" ]] || ! jq -e '.error.code == "authentication_required"' "$verify_root/unauthorized.json" >/dev/null; then
  echo "unauthenticated API request was not rejected" >&2
  exit 1
fi

curl --fail --silent --show-error -D "$verify_root/headers" -o "$verify_root/index.html" "$base_url/"
for header_name in Content-Security-Policy Cross-Origin-Opener-Policy Referrer-Policy X-Content-Type-Options X-Frame-Options; do
  if ! grep -qi "^$header_name:" "$verify_root/headers"; then
    echo "missing security header: $header_name" >&2
    exit 1
  fi
done
asset_path="$(sed -n 's/.*src="\([^"]*\.js\)".*/\1/p' "$verify_root/index.html" | head -n 1)"
if [[ -z "$asset_path" ]]; then
  echo "embedded Web JavaScript asset is missing" >&2
  exit 1
fi
curl --fail --silent --show-error "$base_url$asset_path" -o "$verify_root/app.js"
if ! grep -q '全局审计' "$verify_root/app.js" || ! grep -q '真实环境就绪检查' "$verify_root/app.js"; then
  echo "embedded Web audit or readiness console is missing" >&2
  exit 1
fi

skill_text="$(curl --fail --silent --show-error "$base_url/.well-known/upload-assistant/SKILL.md")"
if [[ "$skill_text" != *"confirm_upload"* ]] || [[ "$skill_text" != *"jobs attempts"* ]] || [[ "$skill_text" != *"jobs replay"* ]] || [[ "$skill_text" != *"/api/v2/audit-events"* ]] || [[ "$skill_text" != *"/api/v2/readiness/live"* ]]; then
  echo "embedded AgentSkill safety or audit contract is missing" >&2
  exit 1
fi

container_identity="$(compose exec -T upload-assistant sh -c 'printf "%s:%s:%s" "$(id -u)" "$(id -g)" "$(stat -c %a /data/master-keys)"')"
if [[ "$container_identity" != "1000:1000:600" ]]; then
  echo "unexpected service identity or master-key permissions: $container_identity" >&2
  exit 1
fi
media_toolchain="$(compose exec -T upload-assistant sh -ec '/usr/local/bin/BDInfo -v >/dev/null; mediainfo --Version >/dev/null; ffmpeg -version >/dev/null 2>&1; ffprobe -version >/dev/null 2>&1; /usr/local/bin/mkbrr version >/dev/null; printf ready')"
if [[ "$media_toolchain" != "ready" ]]; then
  echo "native media toolchain is incomplete" >&2
  exit 1
fi
service_container_id="$(compose ps -q upload-assistant)"
postgres_container_id="$(compose ps -q postgres)"
if ! docker inspect "$service_container_id" | jq -e '.[0].HostConfig.ReadonlyRootfs == true and (.[0].HostConfig.CapDrop | index("ALL")) != null and any(.[0].HostConfig.SecurityOpt[]; startswith("no-new-privileges"))' >/dev/null; then
  echo "service container hardening is incomplete" >&2
  exit 1
fi
if ! docker inspect "$postgres_container_id" | jq -e '((.[0].HostConfig.PortBindings // {}) | length) == 0' >/dev/null; then
  echo "PostgreSQL must not publish a host port" >&2
  exit 1
fi
image_id="$(compose images -q upload-assistant | head -n 1)"
image_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image_id")"
if [[ "$image_platform" != "linux/amd64" ]]; then
  echo "unexpected service image platform: $image_platform" >&2
  exit 1
fi

migration_count="$(compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$UA_POSTGRES_USER" -d "$UA_POSTGRES_DB" -Atc 'SELECT count(*) FROM schema_migrations')"
source_migration_count="$(find "$root_dir/migrations" -maxdepth 1 -type f -name '*.sql' | wc -l | tr -d ' ')"
if [[ "$migration_count" != "$source_migration_count" ]]; then
  echo "database migrations are incomplete: database=$migration_count source=$source_migration_count" >&2
  exit 1
fi

# Normal unit-test runs deliberately skip PostgreSQL integration cases. Exercise
# every durable store and the complete retorrent/daily-candidate runners against
# a separate temporary database so service workers cannot claim fixture jobs.
# PostgreSQL remains unpublished; the host reaches its isolated bridge address.
test_database="${UA_POSTGRES_DB}_verify"
compose exec -T postgres createdb -U "$UA_POSTGRES_USER" -O "$UA_POSTGRES_USER" "$test_database"
postgres_address="$(docker inspect "$postgres_container_id" | jq -er '.[0].NetworkSettings.Networks | to_entries[0].value.IPAddress')"
if [[ ! "$postgres_address" =~ ^[0-9a-fA-F:.]+$ ]]; then
  echo "could not resolve the isolated PostgreSQL container address" >&2
  exit 1
fi
UA_TEST_DATABASE_URL="postgres://$UA_POSTGRES_USER:$UA_POSTGRES_PASSWORD@$postgres_address:5432/$test_database?sslmode=disable" \
  go test ./... -p 1 -count=1
compose exec -T postgres dropdb -U "$UA_POSTGRES_USER" "$test_database"
unset postgres_address test_database

job_request='{"kind":"retorrent","execution_mode":"step","stop_after_step":"source_parse","input":{"source_url":"https://u2.dmhy.org/details.php?id=1","target":"MTEAM","confirm_upload":false}}'
job_headers=(-H "$auth_header" -H 'Content-Type: application/json' -H "Idempotency-Key: local-ready-$verify_suffix")
created_job="$(curl --fail --silent --show-error -X POST "${job_headers[@]}" --data "$job_request" "$base_url/api/v2/jobs")"
replayed_job="$(curl --fail --silent --show-error -X POST "${job_headers[@]}" --data "$job_request" "$base_url/api/v2/jobs")"
job_id="$(jq -er '.job_id' <<<"$created_job")"
if [[ "$job_id" != "$(jq -er '.job_id' <<<"$replayed_job")" ]]; then
  echo "job idempotency replay contract failed" >&2
  exit 1
fi
if ! jq -e '.job.input.confirm_upload == false' <<<"$created_job" >/dev/null; then
  echo "safe create-job request lost confirm_upload=false" >&2
  exit 1
fi

compose restart upload-assistant >/dev/null
wait_ready
persisted_job="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/jobs/$job_id")"
if ! jq -e --arg id "$job_id" '.job_id == $id and .status != "complete"' <<<"$persisted_job" >/dev/null; then
  echo "durable job was lost or incorrectly completed after restart" >&2
  exit 1
fi

jq -n \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg image_platform "$image_platform" \
  --arg job_id "$job_id" \
  --argjson migration_count "$migration_count" \
  --argjson tool_count "$(jq -r '.count' <<<"$tools_json")" \
  '{
    schema_version: 1,
    kind: "upload-assistant.go-v2-local-ready.v1",
    ok: true,
    status: "local_ready",
    generated_at: $generated_at,
    checks: {
      compose: "ready",
      health: "ready",
      authentication: "ready",
      security_headers: "ready",
      non_root_runtime: "ready",
      read_only_root_filesystem: "ready",
      capabilities_dropped: "ready",
      no_new_privileges: "ready",
      postgres_not_published: "ready",
      master_key_permissions: "ready",
      media_toolchain: "ready",
      openapi: "ready",
      tools: "ready",
      agent_skill: "ready",
      web: "ready",
      cli: "ready",
      migrations: "ready",
      postgres_integration_tests: "ready",
      idempotency: "ready",
      restart_persistence: "ready",
      local_live_readiness_handoff: "safe_blocked",
      external_calls_performed: false
    },
    evidence: {
      image_platform: $image_platform,
      migration_count: $migration_count,
      tool_count: $tool_count,
      restart_job_id: $job_id
    },
    blockers: [],
    next_actions: [
      {action: "run_controlled_seedbox_live_validation", requires: ["operator_credentials", "legal_test_resource", "active_approved_rule_fingerprints", "explicit_accept_rules", "explicit_confirm_upload"]}
    ],
    live_validation: {
      status: "blocked_external",
      ok: false,
      blockers: [{code: "external_live_environment_required", message: "U2/CHD→MTEAM、下载器、素材工具和图床的真实闭环需要用户提供合法账号、资源与显式授权；本地验收不会伪造或自动执行。"}]
    }
  }' >"$report_path"

unset api_token auth_header bootstrap_json
jq . "$report_path"
