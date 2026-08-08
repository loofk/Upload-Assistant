import {cleanup, render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import App from "./App";

describe("App authentication boundary", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps the API token entry local to the current session", async () => {
    render(<App />);
    expect(screen.getByRole("heading", {name: "转种工作台"})).toBeInTheDocument();
    const token = screen.getByLabelText("API Token");
    expect(token).toHaveAttribute("type", "password");
    await userEvent.type(token, "ua_test-token-value-that-is-long-enough");
    expect(localStorage.getItem("ua.v2.api-token")).toBeNull();
  });

  it("loads independent integration management through authenticated APIs", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
        : path === "/api/v2/downloaders" ? {downloaders: []}
				: path === "/api/v2/downloader-adapters" ? {adapters: [{adapter: "qbittorrent", display_name: "qBittorrent", runtime_supported: true, credential_fields: [], operations: {probe: true, add_torrent: true, inspect: true, list_files: true, set_limits: true, wait_complete: true, category: true, tags: true, skip_checking: true}}]}
        : path === "/api/v2/image-hosts" ? {image_hosts: []}
        : path === "/api/v2/notification-channels" ? {notification_channels: []}
        : path === "/api/v2/media-managers" ? {media_managers: []}
        : path === "/api/v2/metadata-providers" ? {metadata_providers: []}
        : path === "/api/v2/screenshot-profiles" ? {screenshot_profiles: []}
        : path === "/api/v2/sites" ? {sites: []}
        : {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await userEvent.click(screen.getByRole("button", {name: "配置"}));
    expect(await screen.findByRole("heading", {name: "配置中心"})).toBeInTheDocument();
    expect(await screen.findByText("尚未配置下载器。")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v2/downloaders", expect.objectContaining({credentials: "same-origin"}));
    const downloaderCall = fetchMock.mock.calls.find(([path]) => path === "/api/v2/downloaders");
    expect(new Headers(downloaderCall?.[1]?.headers).get("Authorization")).toBe("Bearer ua_test-token-value-that-is-long-enough");
  });

  it("shows durable step attempts separately from the event chain", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const jobID = "44444444-4444-4444-8444-444444444444";
    const baseJob = {
      id: jobID, kind: "retorrent", status: "blocked", execution_mode: "step", current_step: "target_upload",
      input: {target: "MTEAM"}, blockers: [{code: "remote_outcome_unknown"}], next_actions: [], resume_state: {}, summary: {},
      created_at: "2026-08-08T00:00:00Z", updated_at: "2026-08-08T00:00:00Z",
    };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [baseJob], has_more: false, next_cursor: ""}
        : path === `/api/v2/jobs/${jobID}/summary` ? {...baseJob, ok: false, job_id: jobID, steps: [], artifacts: []}
        : path.startsWith(`/api/v2/jobs/${jobID}/events?`) ? {ok: true, status: "ready", job_id: jobID, events: [], next_cursor: 0}
        : path.startsWith(`/api/v2/jobs/${jobID}/attempts?`) ? {
          ok: true, status: "blocked", job_id: jobID, current_step: "target_upload", has_more: false, next_cursor: "", blockers: baseJob.blockers, next_actions: [],
          attempts: [{
            id: "55555555-5555-4555-8555-555555555555", job_id: jobID,
            step_id: "66666666-6666-4666-8666-666666666666", step_key: "target_upload", step_position: 18,
            number: 2, status: "blocked", input_snapshot: {redacted: true, sha256: "a".repeat(64)},
            output_summary: {}, error_code: "remote_outcome_unknown", error_details: {message: "需要人工核对远端结果"},
            started_at: "2026-08-08T01:00:00Z", finished_at: "2026-08-08T01:00:01Z",
          }],
        } : {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", {name: "尝试 1"}));
    expect(await screen.findByText("上传目标站")).toBeInTheDocument();
    expect(screen.getAllByText(/remote_outcome_unknown/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/sha256:/)).toBeInTheDocument();
  });

  it("shows recursively redacted global configuration and action audits", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
        : path.startsWith("/api/v2/audit-events?") ? {ok: true, status: "ready", has_more: false, next_cursor: "", blockers: [], next_actions: [], audit_events: [{
          id: "88888888-8888-4888-8888-888888888888", actor_type: "worker", actor_id: "fixture",
          action: "downloader.torrent.add", resource_type: "downloader", resource_id: "box",
          payload: {configuration_sha256: "a".repeat(64), api_key: "[REDACTED]"}, created_at: "2026-08-08T00:00:00Z",
        }]}
        : {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await userEvent.click(screen.getByRole("button", {name: "审计"}));
    expect(await screen.findByRole("heading", {name: "全局审计"})).toBeInTheDocument();
    expect(await screen.findByText("downloader.torrent.add")).toBeInTheDocument();
    expect(screen.getByText("downloader · box")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/v2\/audit-events\?/), expect.objectContaining({credentials: "same-origin"}));
  });

  it("shows a local-only readiness handoff without implying live authorization", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
        : path.startsWith("/api/v2/readiness/live?") ? {
          ok: true, status: "configuration_ready", configuration_ready: true,
          external_calls_performed: false, live_upload_authorized: false, source: "U2", target: "MTEAM",
          checks: [{key: "rules.U2", status: "ready", summary: "active rule", evidence: {fingerprint: "a".repeat(64)}}],
          required_confirmations: [{site_code: "U2", fingerprint: "a".repeat(64), obligation_ids: ["manual-review"]}],
          blockers: [], next_actions: [], resume_state: {accept_rules: {U2: {fingerprint: "a".repeat(64), accepted: false}}, confirm_upload: false},
          summary: "本地配置已就绪；未执行任何外部调用。",
        } : {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await userEvent.click(screen.getByRole("button", {name: "就绪检查"}));
    expect(await screen.findByRole("heading", {name: "真实环境就绪检查"})).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "执行本地检查"}));
    expect(await screen.findByRole("heading", {name: "本地配置已就绪"})).toBeInTheDocument();
    expect(screen.getByText("manual-review")).toBeInTheDocument();
    expect(screen.getAllByText("否")).toHaveLength(2);
    expect(screen.getByText("false")).toBeInTheDocument();
    const readinessCall = fetchMock.mock.calls.find(([path]) => String(path).startsWith("/api/v2/readiness/live?"));
    expect(String(readinessCall?.[0])).not.toContain("confirm_upload");
  });

	it("shows Deluge Web endpoint and password-only credential contract", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		const operations = {probe: true, add_torrent: true, inspect: true, list_files: true, set_limits: true, wait_complete: true, category: false, tags: false, skip_checking: false};
		const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
			const path = String(input);
			const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path === "/api/v2/downloaders" ? {downloaders: []}
				: path === "/api/v2/downloader-adapters" ? {adapters: [
					{adapter: "qbittorrent", display_name: "qBittorrent", runtime_supported: true, credential_fields: ["api_key", "password", "username"], operations: {...operations, category: true, tags: true, skip_checking: true}},
					{adapter: "deluge", display_name: "Deluge", runtime_supported: true, credential_fields: ["password"], operations, constraints: ["Web JSON-RPC only"]},
				]}
				: path === "/api/v2/image-hosts" ? {image_hosts: []}
				: path === "/api/v2/notification-channels" ? {notification_channels: []}
				: path === "/api/v2/media-managers" ? {media_managers: []}
				: path === "/api/v2/metadata-providers" ? {metadata_providers: []}
				: path === "/api/v2/screenshot-profiles" ? {screenshot_profiles: []}
				: path === "/api/v2/sites" ? {sites: []} : {};
			return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
		});
		vi.stubGlobal("fetch", fetchMock);
		render(<App />);
		await userEvent.click(screen.getByRole("button", {name: "配置"}));
		await userEvent.selectOptions(await screen.findByLabelText("适配器"), "deluge");
		expect(screen.getByLabelText("服务地址")).toHaveValue("http://host.docker.internal:8112/json");
		expect(screen.getByLabelText("Web 密码（新建必填）")).toHaveAttribute("type", "password");
		expect(screen.queryByLabelText("用户名（与密码同时填写）")).not.toBeInTheDocument();
		expect(screen.getByText("Web JSON-RPC only")).toBeInTheDocument();
	});

  it("shows ranked daily candidates and their safety evidence", async () => {
	sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
	const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
	  const path = String(input);
	  const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
		: path.startsWith("/api/v2/candidates/daily?") ? {ok: true, status: "ready", date: "2026-08-08", count: 1, ready_count: 1, blockers: [], next_actions: [], candidates: [{
		  id: "55555555-5555-4555-8555-555555555555", source_site: "U2", target_site: "MTEAM", source_torrent_id: "60635",
		  recommendation_date: "2026-08-08T00:00:00Z", rank: 1, score: 80, status: "candidate",
		  discovered_at: "2026-08-08T00:00:00Z", expires_at: "2026-08-10T00:00:00Z", updated_at: "2026-08-08T00:00:00Z",
		  payload: {ready: true, source: {title: "Fixture Anime", free: true, size_bytes: 1024}, metadata: {imdb_id: "tt1234567"}, duplicate_check: {duplicate: false}, recommendation_reasons: ["target_duplicate_clear"], risks: [], blockers: []},
		}]}
		: path.startsWith("/api/v2/schedules/daily-candidates?") ? {ok: true, status: "ready", count: 0, schedules: [], blockers: [], next_actions: []}
		: path.startsWith("/api/v2/notifications?") ? {ok: true, status: "ready", count: 0, notifications: [], blockers: [], next_actions: []}
		: {};
	  return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
	});
	vi.stubGlobal("fetch", fetchMock);
	render(<App />);
	await userEvent.click(screen.getByRole("button", {name: "每日候选"}));
	expect(await screen.findByRole("heading", {name: "每日候选"})).toBeInTheDocument();
	expect(await screen.findByRole("heading", {name: "Fixture Anime"})).toBeInTheDocument();
	expect(screen.getByText("目标查重通过")).toBeInTheDocument();
  });

  it("requires reviewed legacy fingerprint before migration", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fingerprint = "a".repeat(64);
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
        : path === "/api/v2/downloaders" ? {downloaders: []}
				: path === "/api/v2/downloader-adapters" ? {adapters: [{adapter: "qbittorrent", display_name: "qBittorrent", runtime_supported: true, credential_fields: [], operations: {probe: true, add_torrent: true, inspect: true, list_files: true, set_limits: true, wait_complete: true, category: true, tags: true, skip_checking: true}}]}
        : path === "/api/v2/image-hosts" ? {image_hosts: []}
        : path === "/api/v2/notification-channels" ? {notification_channels: []}
        : path === "/api/v2/media-managers" ? {media_managers: []}
        : path === "/api/v2/metadata-providers" ? {metadata_providers: []}
        : path === "/api/v2/screenshot-profiles" ? {screenshot_profiles: []}
        : path === "/api/v2/sites" ? {sites: []}
        : path === "/api/v2/migrations/legacy/preview" ? {
          ok: true, status: "ready", source_kind: "upload_assistant_python_config", source_fingerprint: fingerprint,
          source_files: [{path: "config.py", fingerprint, size_bytes: 100}],
          resources: [{kind: "downloader", name: "box", adapter: "qbittorrent", enabled: false, credential_fields: ["username", "password"]}],
          archive: {encrypted: true, retention_days: 30, file_count: 1, uncompressed_bytes: 100, deletes_originals: false, plaintext_available_via_api: false},
          blockers: [], warnings: [{code: "container_loopback_requires_review", resource: "box", message: "需要改为宿主机地址。"}], next_actions: [],
        }
        : path === "/api/v2/migrations/legacy?limit=25" ? {ok: true, status: "ready", imports: [], count: 0, blockers: [], next_actions: []}
        : path === "/api/v2/migrations/legacy" && init?.method === "POST" ? {ok: true, status: "complete", import_id: "import-id", source_fingerprint: fingerprint, blockers: [], next_actions: [], summary: "done", import: {}}
        : {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status: path === "/api/v2/migrations/legacy" ? 201 : 200, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);
    await userEvent.click(screen.getByRole("button", {name: "配置"}));
    await userEvent.click(await screen.findByRole("button", {name: "旧配置迁移"}));
    expect(await screen.findByText("可执行预览")).toBeInTheDocument();
    const execute = screen.getByRole("button", {name: "确认执行迁移"});
    expect(execute).toBeDisabled();
    await userEvent.click(screen.getByLabelText("我已核对源指纹、资源清单和所有 warnings"));
    expect(execute).toBeEnabled();
    await userEvent.click(execute);
    const call = fetchMock.mock.calls.find(([path, init]) => path === "/api/v2/migrations/legacy" && init?.method === "POST");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({source_fingerprint: fingerprint, confirm_import: true});
  });
});
