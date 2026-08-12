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
mkdir -p "$verify_root/downloads" "$verify_root/legacy" "$verify_root/backups" "$(dirname "$report_path")"
chown 1000:1000 "$verify_root/backups"
chmod 0770 "$verify_root/backups"

export UA_POSTGRES_DB="upload_assistant"
export UA_POSTGRES_USER="upload_assistant"
export UA_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export UA_HTTP_PORT="0"
export UA_DOCKER_NETWORK="$verify_project-network"
export UA_DOWNLOADS_HOST_PATH="$verify_root/downloads"
export UA_LEGACY_DATA_HOST_PATH="$verify_root/legacy"
export UA_BACKUPS_SOURCE="$verify_root/backups"

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

fetch_json() {
  local label="$1"
  shift
  local response_file="$verify_root/http-$label.json"
  local status_code
  if ! status_code="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' "$@")"; then
    echo "$label request could not be sent" >&2
    return 1
  fi
  if [[ ! "$status_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "$label request failed with HTTP $status_code: $(jq -c . "$response_file" 2>/dev/null || sed -n '1p' "$response_file")" >&2
    return 1
  fi
  cat "$response_file"
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
adapters_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/adapters")"
audit_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/audit-events?limit=5")"
readiness_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/readiness/live?source=U2&target=MTEAM&downloader=box&image_host=imgbb&screenshot_profile=default&tmdb_provider=tmdb-main&ptgen_provider=ptgen-main")"
cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact audit list --limit 2)"
adapters_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact adapters --kind site)"
readiness_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact readiness live --source U2 --target MTEAM --downloader box --image-host imgbb --screenshot-profile default --tmdb-provider tmdb-main --ptgen-provider ptgen-main)"
rule_import_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact rules import --file - <"$root_dir/scripts/fixtures/go-v2-complete-rule.md")"
rule_revision_id="$(jq -er '.rule_revision_id' <<<"$rule_import_cli_json")"
rule_fingerprint="$(jq -er '.fingerprint' <<<"$rule_import_cli_json")"
rule_list_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact rules list TTG)"
rule_get_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact rules get "$rule_revision_id")"
for rule_section in upload_limit download_limit naming; do
  curl --fail --silent --show-error -X PUT -H "$auth_header" -H 'Content-Type: application/json' \
    --data "$(jq -nc --arg fingerprint "$rule_fingerprint" --arg comment "local Compose verification: $rule_section" '{fingerprint:$fingerprint,decision:"confirmed",comment:$comment}')" \
    "$base_url/api/v2/site-rules/$rule_revision_id/review/$rule_section" >/dev/null
done
rule_review_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/site-rules/$rule_revision_id/review")"
rule_approve_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact rules approve "$rule_revision_id" --fingerprint "$rule_fingerprint" --comment local-compose-verification --confirm)"
rule_activate_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact rules activate "$rule_revision_id" --confirm)"
rule_active_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact rules active TTG)"
rule_sources_json="$(curl --fail --silent --show-error -X PUT -H "$auth_header" -H 'Content-Type: application/json' \
  --data '{"sources":[{"id":"rules","url":"https://rules.example.invalid/ttg","scope":"隔离验收规则页","auth_mode":"none"}],"scope_confirmed":true,"cookie_hosts_confirmed":false}' \
  "$base_url/api/v2/sites/TTG/rule-sources")"

jq -e '.ok == true and .status == "ready" and .checks.database == "ready" and .checks.data_dir == "ready"' <<<"$health_json" >/dev/null
jq -e '.openapi == "3.1.0" and .paths["/api/v2/jobs"] and .paths["/api/v2/jobs/{job_id}/attention"] and .paths["/api/v2/jobs/{job_id}/upload-preview"] and .paths["/api/v2/sites/{site_code}/access-policy"] and .paths["/api/v2/sites/{site_code}/rule-sources"] and .paths["/api/v2/sites/{site_code}/rule-collection-runs"] and .paths["/api/v2/site-rule-collection-runs/{run_id}"] and .paths["/api/v2/site-rule-collection-runs/{run_id}/stream"] and .paths["/api/v2/site-rules/{revision_id}/discard"] and .paths["/api/v2/adapters"] and .paths["/api/v2/audit-events"] and .paths["/api/v2/readiness/live"] and .paths["/api/v2/operational-logs"] and .paths["/api/v2/operational-logs/stream"] and .paths["/api/v2/incidents"] and .paths["/api/v2/diagnostics"] and .paths["/api/v2/operations/overview"] and .paths["/api/v2/api-tokens"] and .paths["/api/v2/backups"] and .components.schemas.RuleSourceSet and .components.schemas.RuleCollectionRun and .components.schemas.RetorrentSummary and .components.schemas.JobAttentionEnvelope and .components.schemas.UploadPreviewEnvelope and .components.schemas.SiteAccessPolicyEnvelope and .components.schemas.AdapterCatalogEnvelope and .components.schemas.LiveReadinessReport and .components.schemas.OperationsSettings and .components.schemas.LLMProviderInput and .components.schemas.BackupPolicyInput' <<<"$openapi_json" >/dev/null
jq -e '.ok == true and .status == "ready" and .count == 68 and any(.tools[]; .name == "get_job_attention") and any(.tools[]; .name == "get_upload_preview") and any(.tools[]; .name == "get_site_access_policy") and any(.tools[]; .name == "get_site_rule_sources") and any(.tools[]; .name == "configure_site_rule_sources") and any(.tools[]; .name == "create_site_rule_collection" and .safety_level == "external_read") and any(.tools[]; .name == "get_site_rule_collection") and any(.tools[]; .name == "analyze_site_rule_revision" and .safety_level == "external_read") and any(.tools[]; .name == "correct_site_rule_hard_gate" and .safety_level == "privileged_write") and any(.tools[]; .name == "discard_site_rule_draft" and .safety_level == "privileged_write") and any(.tools[]; .name == "list_adapter_capabilities") and any(.tools[]; .name == "get_downloader_snapshot" and .safety_level == "external_read") and any(.tools[]; .name == "get_live_readiness") and any(.tools[]; .name == "get_operations_overview") and any(.tools[]; .name == "query_operational_logs") and any(.tools[]; .name == "list_incidents") and any(.tools[]; .name == "get_incident") and any(.tools[]; .name == "create_diagnostic") and any(.tools[]; .name == "get_diagnostic") and all(.tools[]; (.path | startswith("/api/v2/api-tokens") or startswith("/api/v2/llm-providers") or startswith("/api/v2/backups")) | not)' <<<"$tools_json" >/dev/null
jq -e '.ok == true and .status == "ready" and .catalog_version == "upload-assistant.adapter-catalog.v1" and (.catalog_sha256 | test("^[a-f0-9]{64}$")) and .count == 31 and ([.adapters[] | select(.runtime_supported == true)] | length) >= 20 and any(.adapters[]; .id == "image_host/imgbox" and .runtime_supported == true and (.credential_fields | length) == 0 and (.operations | index("upload_image"))) and any(.adapters[]; .id == "image_host/pixhost" and .runtime_supported == true and (.credential_fields | length) == 0 and (.operations | index("upload_image"))) and any(.adapters[]; .id == "notification_channel/telegram_bot" and .runtime_supported == true) and any(.adapters[]; .id == "site/U2" and .runtime_supported == true) and any(.adapters[]; .id == "site/AUDIENCES" and .runtime_supported == false and (.unavailable_reason | length) > 0)' <<<"$adapters_json" >/dev/null
jq -e '.ok == true and .status == "ready" and (.audit_events | type == "array")' <<<"$audit_json" >/dev/null
jq -e '.ok == true and .status == "ready" and (.audit_events | type == "array")' <<<"$cli_json" >/dev/null
jq -e '.ok == true and .status == "ready" and .count == 11 and (.catalog_sha256 | test("^[a-f0-9]{64}$")) and all(.adapters[]; .kind == "site")' <<<"$adapters_cli_json" >/dev/null
jq -e '.status == "blocked" and .configuration_ready == false and .external_calls_performed == false and .live_upload_authorized == false and .resume_state.confirm_upload == false and (.blockers | length > 0)' <<<"$readiness_json" >/dev/null
jq -e '.status == "blocked" and .external_calls_performed == false and .live_upload_authorized == false and .resume_state.confirm_upload == false' <<<"$readiness_cli_json" >/dev/null
jq -e --arg id "$rule_revision_id" --arg fingerprint "$rule_fingerprint" '.status == "draft" and .rule_revision_id == $id and .fingerprint == $fingerprint' <<<"$rule_import_cli_json" >/dev/null
jq -e --arg id "$rule_revision_id" 'any(.revisions[]; .id == $id and .status == "draft")' <<<"$rule_list_cli_json" >/dev/null
jq -e --arg id "$rule_revision_id" --arg fingerprint "$rule_fingerprint" '.rule_revision_id == $id and .fingerprint == $fingerprint and .status == "draft"' <<<"$rule_get_cli_json" >/dev/null
jq -e --arg id "$rule_revision_id" '.review.revision_id == $id and .review.confirmed_count == 3 and .review.required_count == 3 and .review.approval_ready == true and (.blockers | length == 0)' <<<"$rule_review_json" >/dev/null
jq -e --arg id "$rule_revision_id" '.rule_revision_id == $id and .status == "approved"' <<<"$rule_approve_cli_json" >/dev/null
jq -e --arg id "$rule_revision_id" '.rule_revision_id == $id and .status == "approved"' <<<"$rule_activate_cli_json" >/dev/null
jq -e --arg id "$rule_revision_id" --arg fingerprint "$rule_fingerprint" '.rule_revision_id == $id and .fingerprint == $fingerprint and .status == "approved"' <<<"$rule_active_cli_json" >/dev/null
jq -e '.ok == true and .status == "ready" and .source_set.site_code == "TTG" and (.source_set.sources | length) == 1 and .source_set.sources[0].id == "rules" and .source_set.sources[0].auth_mode == "none" and .source_set.scope_confirmed == true and .source_set.cookie_hosts_confirmed == false and .source_set.cookie_configured == false and .source_set.cookie_required == false and (.source_set.fingerprint | test("^[a-f0-9]{64}$")) and (.blockers | length) == 0' <<<"$rule_sources_json" >/dev/null

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
if ! grep -q '全局审计' "$verify_root/app.js" || ! grep -q '执行本地检查' "$verify_root/app.js" || ! grep -q '运维中心' "$verify_root/app.js" || ! grep -q '站点规则编译与硬门禁' "$verify_root/app.js" || ! grep -q 'Private identity 仅显示一次' "$verify_root/app.js"; then
  echo "embedded Web audit, readiness, or operations console is missing" >&2
  exit 1
fi

skill_text="$(curl --fail --silent --show-error "$base_url/.well-known/upload-assistant/SKILL.md")"
if [[ "$skill_text" != *"confirm_upload"* ]] || [[ "$skill_text" != *"jobs attempts"* ]] || [[ "$skill_text" != *"jobs replay"* ]] || [[ "$skill_text" != *"resume_state.reconciliation"* ]] || [[ "$skill_text" != *"/api/v2/audit-events"* ]] || [[ "$skill_text" != *"/api/v2/readiness/live"* ]] || [[ "$skill_text" != *"query_operational_logs"* ]] || [[ "$skill_text" != *"age X25519"* ]] || [[ "$skill_text" != *"private identity"* ]]; then
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
backup_toolchain="$(compose exec -T upload-assistant sh -ec 'age --version >/dev/null; age-keygen --version >/dev/null; pg_dump --version | grep -q "17\."; pg_restore --version | grep -q "17\."; printf ready')"
if [[ "$backup_toolchain" != "ready" ]]; then
  echo "age or PostgreSQL 17 backup toolchain is incomplete" >&2
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

# Exercise the operations APIs against the isolated service. The incident and
# artifact are local fixtures; no tracker, downloader, image host, notifier, or
# model endpoint is contacted.
fixture_incident_fingerprint="local-ready-incident-$verify_suffix"
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$UA_POSTGRES_USER" -d "$UA_POSTGRES_DB" \
  -v fingerprint="$fixture_incident_fingerprint" -v job_id="$job_id" >/dev/null <<'SQL'
INSERT INTO incidents(severity,kind,fingerprint,title,summary,job_id,evidence)
VALUES('warning','local_verifier',:'fingerprint','隔离验收异常','仅用于本地运维链路验收',:'job_id','{"fixture":true}');
SQL

jq -n --arg job_id "$job_id" '{kind:"local_ready_artifact",job_id:$job_id,value:"before-backup"}' >"$verify_root/local-ready-artifact.json"
artifact_size="$(stat -c %s "$verify_root/local-ready-artifact.json")"
artifact_sha256="$(sha256sum "$verify_root/local-ready-artifact.json" | awk '{print $1}')"
compose exec -T upload-assistant sh -ec 'umask 077; tee /data/artifacts/local-ready-artifact.json >/dev/null' <"$verify_root/local-ready-artifact.json"
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$UA_POSTGRES_USER" -d "$UA_POSTGRES_DB" \
  -v job_id="$job_id" -v artifact_size="$artifact_size" -v artifact_sha256="$artifact_sha256" >/dev/null <<'SQL'
INSERT INTO artifacts(job_id,kind,storage_backend,storage_path,filename,mime_type,size_bytes,sha256,metadata,expires_at)
VALUES(:'job_id','local_ready_fixture','local','artifacts/local-ready-artifact.json','local-ready-artifact.json','application/json',:artifact_size,:'artifact_sha256','{"fixture":true}',now()+interval '30 days');
SQL

sleep 2
if ! overview_json="$(fetch_json operations-overview -H "$auth_header" "$base_url/api/v2/operations/overview")"; then
  compose logs --no-color upload-assistant >&2 || true
  exit 1
fi
if ! logs_json="$(fetch_json operational-logs -H "$auth_header" "$base_url/api/v2/operational-logs?limit=100")"; then
  compose logs --no-color upload-assistant >&2 || true
  exit 1
fi
if ! incidents_json="$(fetch_json incidents -H "$auth_header" "$base_url/api/v2/incidents?kind=local_verifier&limit=10")"; then
  compose logs --no-color upload-assistant >&2 || true
  exit 1
fi
operations_cli_json="$(compose exec -T -e UA_API_URL=http://127.0.0.1:8080 -e UA_API_TOKEN="$api_token" upload-assistant upload-assistant cli --compact operations overview)"
jq -e '.ok == true and .status == "ready" and (.overview.filesystems | length) == 3 and .overview.application_version != ""' <<<"$overview_json" >/dev/null
jq -e '.ok == true and (.operational_logs | length) > 0 and all(.operational_logs[]; (.attributes | tostring | contains("Bearer ") | not))' <<<"$logs_json" >/dev/null
jq -e --arg fingerprint "$fixture_incident_fingerprint" '.ok == true and any(.incidents[]; .fingerprint == $fingerprint)' <<<"$incidents_json" >/dev/null
jq -e '.ok == true and .status == "ready" and (.overview.filesystems | length) == 3' <<<"$operations_cli_json" >/dev/null

token_create_json="$(fetch_json token-create -X POST -H "$auth_header" -H 'Content-Type: application/json' --data '{"name":"local-ready-readonly","scopes":["operations:read","logs:read"]}' "$base_url/api/v2/api-tokens")"
restricted_token="$(jq -er '.api_token.token' <<<"$token_create_json")"
restricted_token_id="$(jq -er '.api_token.id' <<<"$token_create_json")"
curl --fail --silent --show-error -H "Authorization: Bearer $restricted_token" "$base_url/api/v2/operations/overview" >/dev/null
fetch_json token-revoke -X DELETE -H "$auth_header" "$base_url/api/v2/api-tokens/$restricted_token_id" >/dev/null
revoked_status="$(curl --silent --show-error -o "$verify_root/revoked-token.json" -w '%{http_code}' -H "Authorization: Bearer $restricted_token" "$base_url/api/v2/operations/overview")"
unset restricted_token
if [[ "$revoked_status" != "401" ]] || ! jq -e '.error.code == "invalid_token"' "$verify_root/revoked-token.json" >/dev/null; then
  echo "revoked API token remained usable" >&2
  exit 1
fi

rule_relative_path="$(compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$UA_POSTGRES_USER" -d "$UA_POSTGRES_DB" -v revision_id="$rule_revision_id" -At <<'SQL'
SELECT markdown_path FROM site_rule_revisions WHERE id=:'revision_id';
SQL
)"
master_key_sha256="$(compose exec -T upload-assistant sha256sum /data/master-keys | awk '{print $1}')"
rule_file_sha256="$(compose exec -T -e RULE_PATH="$rule_relative_path" upload-assistant sh -ec 'sha256sum "/data/rules/$RULE_PATH"' | awk '{print $1}')"

backup_policy_json="$(fetch_json backup-policy -X PUT -H "$auth_header" -H 'Content-Type: application/json' --data '{"enabled":true,"schedule":"30 3 * * *","retention_count":7,"generate_identity":true}' "$base_url/api/v2/backups/policy")"
backup_identity="$(jq -er '.identity_once' <<<"$backup_policy_json")"
if [[ "$backup_identity" != AGE-SECRET-KEY-* ]] || ! jq -e '.policy.enabled == true and (.policy.recipient | startswith("age1"))' <<<"$backup_policy_json" >/dev/null; then
  echo "age X25519 policy did not return a one-time identity and public recipient" >&2
  exit 1
fi
printf '%s\n' "$backup_identity" | compose exec -T upload-assistant sh -ec 'umask 077; tee /backups/local-ready-identity.txt >/dev/null'
unset backup_identity
compose exec -T upload-assistant sh -ec 'umask 077; age-keygen > /backups/local-ready-wrong-identity.txt 2>/dev/null'
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$UA_POSTGRES_USER" -d "$UA_POSTGRES_DB" -v job_id="$job_id" >/dev/null <<'SQL'
UPDATE jobs SET status='running' WHERE id=:'job_id';
SQL
deferred_backup_json="$(fetch_json backup-deferred -X POST -H "$auth_header" "$base_url/api/v2/backups")"
jq -e '.status == "deferred" and .blockers[0].code == "active_write_jobs"' <<<"$deferred_backup_json" >/dev/null
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$UA_POSTGRES_USER" -d "$UA_POSTGRES_DB" -v job_id="$job_id" >/dev/null <<'SQL'
UPDATE jobs SET status='blocked' WHERE id=:'job_id';
SQL
if ! backup_json="$(fetch_json backup-create --max-time 180 -X POST -H "$auth_header" "$base_url/api/v2/backups")"; then
  echo "persisted backup runs: $(fetch_json backup-runs -H "$auth_header" "$base_url/api/v2/backups/runs" || true)" >&2
  compose logs --no-color upload-assistant >&2 || true
  exit 1
fi
backup_id="$(jq -er '.backup_id' <<<"$backup_json")"
bundle_path="$(jq -er '.backup.bundle_path' <<<"$backup_json")"
if [[ "$bundle_path" != /backups/*.age ]] || ! jq -e '.status == "complete" and (.backup.bundle_sha256 | test("^[a-f0-9]{64}$")) and .backup.size_bytes > 0' <<<"$backup_json" >/dev/null; then
  echo "encrypted backup did not complete with a durable receipt" >&2
  exit 1
fi
backup_verify_json="$(fetch_json backup-verify -X POST -H "$auth_header" "$base_url/api/v2/backups/$backup_id/verify")"
jq -e '.status == "verified" and .backup.verified_at != null' <<<"$backup_verify_json" >/dev/null
if compose exec -T upload-assistant upload-assistant admin backup restore --bundle "$bundle_path" --identity /backups/local-ready-identity.txt --confirm >"$verify_root/running-restore.out" 2>"$verify_root/running-restore.err"; then
  echo "offline restore was accepted while the service lock was held" >&2
  exit 1
fi

settings_before_json="$(fetch_json operations-settings -H "$auth_header" "$base_url/api/v2/operations/settings")"
queue_warning_before="$(jq -er '.settings.queue_warning_count' <<<"$settings_before_json")"
settings_mutation="$(jq -c '.settings | .queue_warning_count=99' <<<"$settings_before_json")"
fetch_json operations-settings-update -X PUT -H "$auth_header" -H 'Content-Type: application/json' --data "$settings_mutation" "$base_url/api/v2/operations/settings" >/dev/null
jq -n '{kind:"local_ready_artifact",value:"after-backup"}' >"$verify_root/local-ready-artifact-mutated.json"
compose exec -T upload-assistant sh -ec 'umask 077; tee /data/artifacts/local-ready-artifact.json >/dev/null' <"$verify_root/local-ready-artifact-mutated.json"
printf '%s\n' '# mutated after encrypted backup' >"$verify_root/rule-mutated.md"
compose exec -T -e RULE_PATH="$rule_relative_path" upload-assistant sh -ec 'umask 077; tee "/data/rules/$RULE_PATH" >/dev/null' <"$verify_root/rule-mutated.md"
printf '1:%s\n' "$(openssl rand -base64 32)" >"$verify_root/master-keys-mutated"
chmod 600 "$verify_root/master-keys-mutated"
compose exec -T upload-assistant sh -ec 'umask 077; tee /data/master-keys >/dev/null' <"$verify_root/master-keys-mutated"

compose stop upload-assistant >/dev/null
bundle_filename="$(basename "$bundle_path")"
cp "$verify_root/backups/$bundle_filename" "$verify_root/backups/local-ready-corrupt.age"
cp "$verify_root/backups/$bundle_filename.receipt.json" "$verify_root/backups/local-ready-corrupt.age.receipt.json"
printf 'corrupt' >>"$verify_root/backups/local-ready-corrupt.age"
if compose run --rm --no-deps upload-assistant admin backup restore --bundle /backups/local-ready-corrupt.age --identity /backups/local-ready-identity.txt --confirm >"$verify_root/corrupt-restore.out" 2>"$verify_root/corrupt-restore.err"; then
  echo "corrupted encrypted bundle was accepted" >&2
  exit 1
fi
if compose run --rm --no-deps upload-assistant admin backup restore --bundle "$bundle_path" --identity /backups/local-ready-wrong-identity.txt --confirm >"$verify_root/wrong-key-restore.out" 2>"$verify_root/wrong-key-restore.err"; then
  echo "wrong age identity was accepted" >&2
  exit 1
fi
cp "$verify_root/backups/$bundle_filename" "$verify_root/backups/local-ready-version.age"
jq '.application_version="incompatible-fixture"' "$verify_root/backups/$bundle_filename.receipt.json" >"$verify_root/backups/local-ready-version.age.receipt.json"
if compose run --rm --no-deps upload-assistant admin backup restore --bundle /backups/local-ready-version.age --identity /backups/local-ready-identity.txt --confirm >"$verify_root/version-restore.out" 2>"$verify_root/version-restore.err"; then
  echo "incompatible backup version was accepted" >&2
  exit 1
fi
restore_json="$(compose run --rm --no-deps upload-assistant admin backup restore --bundle "$bundle_path" --identity /backups/local-ready-identity.txt --confirm)"
jq -e '.ok == true and .status == "complete"' <<<"$restore_json" >/dev/null
compose start upload-assistant >/dev/null
wait_ready

settings_after_json="$(curl --fail --silent --show-error -H "$auth_header" "$base_url/api/v2/operations/settings")"
if [[ "$(jq -er '.settings.queue_warning_count' <<<"$settings_after_json")" != "$queue_warning_before" ]]; then
  echo "database data was not restored from the encrypted backup" >&2
  exit 1
fi
restored_master_key_sha256="$(compose exec -T upload-assistant sha256sum /data/master-keys | awk '{print $1}')"
restored_rule_file_sha256="$(compose exec -T -e RULE_PATH="$rule_relative_path" upload-assistant sh -ec 'sha256sum "/data/rules/$RULE_PATH"' | awk '{print $1}')"
restored_artifact_sha256="$(compose exec -T upload-assistant sha256sum /data/artifacts/local-ready-artifact.json | awk '{print $1}')"
if [[ "$restored_master_key_sha256" != "$master_key_sha256" ]] || [[ "$restored_rule_file_sha256" != "$rule_file_sha256" ]] || [[ "$restored_artifact_sha256" != "$artifact_sha256" ]]; then
  echo "master key, rule document, or artifact did not survive offline restore" >&2
  exit 1
fi
rollback_receipt_count="$(compose exec -T upload-assistant sh -ec 'find /data/restore-rollbacks -name restore-receipt.json -type f | wc -l')"
if [[ "$rollback_receipt_count" -lt 1 ]]; then
  echo "offline restore did not preserve rollback evidence" >&2
  exit 1
fi

jq -n \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg image_platform "$image_platform" \
  --arg job_id "$job_id" \
  --arg backup_id "$backup_id" \
  --argjson rollback_receipt_count "$rollback_receipt_count" \
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
      backup_toolchain: "ready",
      openapi: "ready",
      tools: "ready",
      agent_skill: "ready",
      web: "ready",
      cli: "ready",
      cli_rule_lifecycle: "ready",
      rule_source_configuration: "ready_no_external_call",
      migrations: "ready",
      postgres_integration_tests: "ready",
      idempotency: "ready",
      restart_persistence: "ready",
      operational_logs: "ready",
      incidents: "ready",
      evidence_bound_diagnostics: "ready_httptest",
      capacity_alerts: "ready_integration",
      api_token_lifecycle: "ready",
      encrypted_backup: "ready",
      offline_restore: "ready",
      restore_rollback_preserved: "ready",
      local_live_readiness_handoff: "safe_blocked",
      external_calls_performed: false
    },
    evidence: {
      image_platform: $image_platform,
      migration_count: $migration_count,
      tool_count: $tool_count,
      restart_job_id: $job_id,
      backup_id: $backup_id,
      restore_rollback_receipt_count: $rollback_receipt_count
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
