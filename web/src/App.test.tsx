import {cleanup, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import App from "./App";

describe("App authentication boundary", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    delete document.documentElement.dataset.theme;
    delete document.documentElement.dataset.themeMode;
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps the API token entry local to the current session", async () => {
    render(<App />);
    expect(screen.getByRole("heading", {name: "转种工作台"})).toBeInTheDocument();
    const token = screen.getByLabelText("API Token");
    expect(token).toHaveAttribute("type", "password");
		expect(screen.getByText("第一次使用？查看 Token 获取方式")).toBeInTheDocument();
    await userEvent.type(token, "ua_test-token-value-that-is-long-enough");
    expect(localStorage.getItem("ua.v2.api-token")).toBeNull();
  });

	it("switches between system, light, and dark themes and persists the preference", async () => {
		render(<App />);
		const selector = screen.getByLabelText("主题");
		expect(selector).toHaveValue("system");
		await userEvent.selectOptions(selector, "light");
		await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "light"));
		expect(localStorage.getItem("ua.v2.theme")).toBe("light");
		await userEvent.selectOptions(selector, "dark");
		await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "dark"));
		expect(localStorage.getItem("ua.v2.theme")).toBe("dark");
		cleanup();
		render(<App />);
		expect(screen.getByLabelText("主题")).toHaveValue("dark");
	});

	it("validates a typed token before persisting or opening the console", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			error: {code: "invalid_token", detail: "invalid token"},
		}), {status: 401, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		render(<App />);
		await userEvent.type(screen.getByLabelText("API Token"), "ua_invalid-token-value-that-is-long-enough");
		await userEvent.click(screen.getByRole("button", {name: "验证并进入"}));
		expect(await screen.findByRole("alert")).toHaveTextContent("Token 未通过验证");
		expect(sessionStorage.getItem("ua.v2.api-token")).toBeNull();
		expect(screen.getByRole("heading", {name: "转种工作台"})).toBeInTheDocument();
	});

	it("stores a server-validated token only in the current tab session", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", jobs: [], has_more: false, next_cursor: "",
		}), {status: 200, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		render(<App />);
		const token = "ua_test-token-value-that-is-long-enough";
		await userEvent.type(screen.getByLabelText("API Token"), token);
		await userEvent.click(screen.getByRole("button", {name: "验证并进入"}));
		await waitFor(() => expect(sessionStorage.getItem("ua.v2.api-token")).toBe(token));
		expect(localStorage.getItem("ua.v2.api-token")).toBeNull();
		expect(await screen.findByText("已连接")).toBeInTheDocument();
	});

	it("exposes the primary navigation as an escape-dismissible compact menu", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", jobs: [], has_more: false, next_cursor: "",
		}), {status: 200, headers: {"Content-Type": "application/json"}})));
		render(<App />);
		const navigation = await screen.findByRole("navigation", {name: "主导航"});
		expect(navigation).not.toHaveClass("open");
		await userEvent.click(screen.getByRole("button", {name: "打开主菜单"}));
		expect(navigation).toHaveClass("open");
		expect(screen.getAllByRole("button", {name: "关闭主菜单"})).toHaveLength(2);
		await userEvent.keyboard("{Escape}");
		await waitFor(() => expect(navigation).not.toHaveClass("open"));
	});

	it("traps focus in the create dialog and restores it after escape", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", jobs: [], has_more: false, next_cursor: "",
		}), {status: 200, headers: {"Content-Type": "application/json"}})));
		render(<App />);
		const trigger = await screen.findByRole("button", {name: "新建"});
		await userEvent.click(trigger);
		expect(screen.getByRole("dialog", {name: "创建转种任务"})).toBeInTheDocument();
		expect(screen.getByLabelText("源站详情链接")).toHaveFocus();
		await userEvent.keyboard("{Escape}");
		expect(screen.queryByRole("dialog", {name: "创建转种任务"})).not.toBeInTheDocument();
		expect(trigger).toHaveFocus();
	});

	it("returns an expired stored token to the connection screen", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_expired-token-value-that-is-long-enough");
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			error: {code: "invalid_token", detail: "expired token"},
		}), {status: 401, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		render(<App />);
		expect(await screen.findByRole("heading", {name: "转种工作台"})).toBeInTheDocument();
		expect(screen.getByRole("alert")).toHaveTextContent("请重新输入服务签发的 Token");
		expect(sessionStorage.getItem("ua.v2.api-token")).toBeNull();
	});

	it("keeps human and Agent help outside the primary navigation", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", jobs: [], has_more: false, next_cursor: "",
		}), {status: 200, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		render(<App />);
		const primaryNavigation = await screen.findByRole("navigation", {name: "主导航"});
		expect(within(primaryNavigation).queryByText("使用指南")).not.toBeInTheDocument();
		await userEvent.click(screen.getByText("帮助"));
		await userEvent.click(screen.getByRole("menuitem", {name: /人工使用指南/}));
		expect(await screen.findByRole("heading", {name: "帮助中心"})).toBeInTheDocument();
		expect(screen.getByRole("heading", {name: "配置站点与规则"})).toBeInTheDocument();
		expect(window.location.pathname).toBe("/app/help/operator");
		await userEvent.click(screen.getByRole("button", {name: "Agent 接入"}));
		expect(await screen.findByRole("heading", {name: "推荐接入方式"})).toBeInTheDocument();
		expect(screen.getAllByText("/openapi.json")).toHaveLength(2);
		expect(screen.getByText(/accept_rules \+ confirm_upload=true/)).toBeInTheDocument();
		expect(window.location.pathname).toBe("/app/help/agent");
	});

	it("restores the main page and configuration subtab from the URL after refresh", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		window.history.replaceState({}, "", "/app/configuration/rules");
		const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
			const path = String(input);
			const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path === "/api/v2/adapters" ? {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 0, blockers: [], next_actions: [], adapters: []}
				: path === "/api/v2/downloaders" ? {downloaders: []}
				: path === "/api/v2/downloader-adapters" ? {adapters: []}
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
		expect(await screen.findByRole("heading", {name: "配置中心"})).toBeInTheDocument();
		const configNavigation = screen.getByLabelText("配置分类");
		expect(within(configNavigation).getByRole("button", {name: "站点规则 0"})).toHaveAttribute("aria-current", "page");
		expect(screen.getByRole("button", {name: "站点规则 0"})).toHaveClass("active");
		await userEvent.click(screen.getByRole("button", {name: "截图 0"}));
		expect(window.location.pathname).toBe("/app/configuration/screenshots");
		cleanup();
		render(<App />);
		expect(await screen.findByRole("button", {name: "截图 0"})).toHaveClass("active");
	});

	it("exposes logs and AI diagnostics as deep-linkable operations tabs", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		window.history.replaceState({}, "", "/app/operations/logs");
		const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
			const path = String(input);
			const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path.startsWith("/api/v2/operational-logs?") ? {operational_logs: []}
				: path === "/api/v2/diagnostics?limit=100" ? {diagnostics: []}
				: path === "/api/v2/llm-providers" ? {llm_providers: []}
				: {};
			return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
		});
		vi.stubGlobal("fetch", fetchMock);
		render(<App />);
		expect(await screen.findByRole("heading", {name: "运维中心"})).toBeInTheDocument();
		expect(screen.getByRole("tab", {name: "运行日志"})).toHaveAttribute("aria-selected", "true");
		expect(screen.getByRole("button", {name: "运维中心"})).toHaveAttribute("aria-current", "page");
		await userEvent.click(screen.getByRole("tab", {name: "AI 诊断"}));
		expect(window.location.pathname).toBe("/app/operations/diagnostics");
		expect(await screen.findByRole("heading", {name: "生成证据诊断"})).toBeInTheDocument();
	});

  it("loads independent integration management through authenticated APIs", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path === "/api/v2/adapters" ? {ok: true, status: "ready", catalog_version: "upload-assistant.adapter-catalog.v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [{id: "site/U2", kind: "site", adapter: "nexusphp", display_name: "U2", site_code: "U2", runtime_supported: true, operations: ["inspect_source"], credential_fields: ["cookie"], safety_gates: ["active_rule_revision"], constraints: ["source-only"]}]}
        : path === "/api/v2/downloaders" ? {downloaders: []}
				: path === "/api/v2/downloader-adapters" ? {adapters: [{adapter: "qbittorrent", display_name: "qBittorrent", runtime_supported: true, credential_fields: [], operations: {probe: true, add_torrent: true, inspect: true, list_torrents: true, list_files: true, set_limits: true, wait_complete: true, category: true, tags: true, skip_checking: true}}]}
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
		await userEvent.click(screen.getByRole("button", {name: "能力 1"}));
		expect(await screen.findByText("U2 · U2")).toBeInTheDocument();
		expect(screen.getByText(/contract sha256: a{64}/)).toBeInTheDocument();
  });

	it("loads a saved downloader for editing and exposes probe progress and completion", async () => {
		sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
		let finishProbe: ((response: Response) => void) | undefined;
		const probeResponse = new Promise<Response>((resolve) => { finishProbe = resolve; });
		const operations = {probe: true, add_torrent: true, inspect: true, list_torrents: true, list_files: true, set_limits: true, wait_complete: true, category: true, tags: true, skip_checking: true};
		const downloader = {
			id: "11111111-1111-4111-8111-111111111111", name: "box", adapter: "qbittorrent", enabled: true, network_class: "seedbox",
			config: {endpoint: "http://host.docker.internal:8080", timeout_seconds: 30, options: {}},
			credential_fields: ["password", "username"], path_mappings: [{remote_path: "/remote/downloads", local_path: "/downloads", priority: 120}],
			health_status: "unknown", created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z",
			adapter_capability: {adapter: "qbittorrent", display_name: "qBittorrent", runtime_supported: true, credential_fields: ["password", "username"], operations},
		};
		const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
			const path = String(input);
			if (path === "/api/v2/downloaders/box/probe" && init?.method === "POST") return probeResponse;
			const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path === "/api/v2/adapters" ? {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 0, blockers: [], next_actions: [], adapters: []}
				: path === "/api/v2/downloaders" ? {downloaders: [downloader]}
				: path === "/api/v2/downloader-adapters" ? {adapters: [downloader.adapter_capability]}
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
		await userEvent.click(await screen.findByRole("button", {name: "配置"}));
		await userEvent.click(await screen.findByRole("button", {name: "编辑配置"}));
		expect(screen.getByRole("heading", {name: "编辑下载器 · box"})).toBeInTheDocument();
		expect(screen.getByLabelText("配置名称")).toHaveValue("box");
		expect(screen.getByLabelText("配置名称")).toHaveAttribute("readonly");
		expect(screen.getByLabelText("服务地址")).toHaveValue("http://host.docker.internal:8080");
		expect(screen.getByLabelText("网络类型")).toHaveValue("seedbox");
			expect(screen.getByLabelText("远程路径 1")).toHaveValue("/remote/downloads");
			expect(screen.getByText(/留空会保留已有值/)).toBeInTheDocument();
			expect(screen.getByRole("button", {name: "路径映射说明"})).toHaveAttribute("aria-describedby", "path-mapping-tooltip");
			expect(document.getElementById("path-mapping-tooltip")).toHaveTextContent("下载器路径来自 Web API");
			expect(document.getElementById("path-mapping-tooltip")).toHaveTextContent("/mnt/media/downloads → /downloads");

		await userEvent.click(screen.getByRole("button", {name: "显式探测"}));
		expect(await screen.findByRole("button", {name: "探测中…"})).toBeDisabled();
		expect(screen.getByText("正在连接下载器并读取版本信息…")).toBeInTheDocument();
		finishProbe?.(new Response(JSON.stringify({ok: true, status: "ready", downloader: "box", probe: {webapi_version: "2.14.1"}}), {status: 200, headers: {"Content-Type": "application/json"}}));
		expect(await screen.findByText("连接成功，健康状态与审计证据已更新。")).toBeInTheDocument();

		await userEvent.clear(screen.getByLabelText("服务地址"));
		await userEvent.type(screen.getByLabelText("服务地址"), "http://host.docker.internal:8081");
		await userEvent.click(screen.getByRole("button", {name: "保存配置"}));
		await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => path === "/api/v2/downloaders/box" && init?.method === "PUT")).toBe(true));
		const updateCall = fetchMock.mock.calls.find(([path, init]) => path === "/api/v2/downloaders/box" && init?.method === "PUT");
		expect(JSON.parse(String(updateCall?.[1]?.body))).toMatchObject({
			adapter: "qbittorrent", enabled: true, network_class: "seedbox", config: {endpoint: "http://host.docker.internal:8081"}, credentials: {},
			path_mappings: [{remote_path: "/remote/downloads", local_path: "/downloads", priority: 120}],
		});
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
    expect(screen.queryByRole("button", {name: "重放新任务"})).not.toBeInTheDocument();
		expect(screen.getByRole("button", {name: "对账后续跑"})).toBeDisabled();
		expect(screen.getByText("必须完成远端对账")).toBeInTheDocument();
  });

  it("creates a safety-reset replay from an eligible job", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const originalID = "44444444-4444-4444-8444-444444444444";
    const replayID = "77777777-7777-4777-8777-777777777777";
    const job = (id: string, status: "blocked" | "queued") => ({
      id, kind: "retorrent", status, execution_mode: "step", current_step: "source_parse",
      replay_of_job_id: id === replayID ? originalID : undefined,
      input: {target: "MTEAM"}, blockers: status === "blocked" ? [{code: "credential_required"}] : [],
      next_actions: [], resume_state: {}, summary: {}, created_at: "2026-08-08T00:00:00Z", updated_at: "2026-08-08T00:00:00Z",
    });
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      let payload: object;
      let status = 200;
      if (path.startsWith("/api/v2/jobs?")) payload = {ok: true, status: "ready", jobs: [job(originalID, "blocked")], has_more: false, next_cursor: ""};
      else if (path === `/api/v2/jobs/${originalID}/replay` && init?.method === "POST") {
        payload = {ok: true, status: "queued", job_id: replayID, replay_of_job_id: originalID, job: job(replayID, "queued"), blockers: [], next_actions: [], resume_state: {}, summary: {}};
        status = 202;
      } else if (path.endsWith("/summary")) {
        const current = path.includes(replayID) ? job(replayID, "queued") : job(originalID, "blocked");
        payload = {...current, ok: current.status !== "blocked", job_id: current.id, steps: [], artifacts: []};
      } else if (path.includes("/events?")) payload = {ok: true, status: "ready", events: [], next_cursor: 0};
      else if (path.includes("/attempts?")) payload = {ok: true, status: "ready", attempts: [], has_more: false, next_cursor: "", blockers: [], next_actions: []};
      else payload = {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", {randomUUID: () => "88888888-8888-4888-8888-888888888888"});
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", {name: "重放新任务"}));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => path === `/api/v2/jobs/${originalID}/replay` && init?.method === "POST")).toBe(true));
    const replayCall = fetchMock.mock.calls.find(([path, init]) => path === `/api/v2/jobs/${originalID}/replay` && init?.method === "POST");
    expect(new Headers(replayCall?.[1]?.headers).get("Idempotency-Key")).toBe("88888888-8888-4888-8888-888888888888");
    expect(JSON.parse(String(replayCall?.[1]?.body))).toEqual({execution_mode: "step"});
    expect(await screen.findByText(new RegExp(replayID))).toBeInTheDocument();
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
		await userEvent.click(screen.getByRole("button", {name: "环境检查"}));
		expect(await screen.findByRole("heading", {name: "环境检查"})).toBeInTheDocument();
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
		const operations = {probe: true, add_torrent: true, inspect: true, list_torrents: true, list_files: true, set_limits: true, wait_complete: true, category: false, tags: false, skip_checking: false};
		const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
			const path = String(input);
			const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path === "/api/v2/adapters" ? {ok: true, status: "ready", catalog_version: "upload-assistant.adapter-catalog.v1", catalog_sha256: "a".repeat(64), count: 0, blockers: [], next_actions: [], adapters: []}
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
		await userEvent.click(await screen.findByRole("button", {name: "新增下载器"}));
		await userEvent.selectOptions(await screen.findByLabelText("适配器"), "deluge");
		await userEvent.click(screen.getByRole("checkbox", {name: /自定义连接/}));
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

  it("requires operator evidence before retrying an unknown Discord delivery", async () => {
	sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
	const notificationID = "77777777-7777-4777-8777-777777777777";
	const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
	  const path = String(input);
	  const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
		: path.startsWith("/api/v2/candidates/daily?") ? {ok: true, status: "ready", date: "2026-08-08", count: 0, ready_count: 0, blockers: [], next_actions: [], candidates: []}
		: path.startsWith("/api/v2/schedules/daily-candidates?") ? {ok: true, status: "ready", count: 0, schedules: [], blockers: [], next_actions: []}
		: path.startsWith("/api/v2/notifications?") ? {ok: true, status: "ready", count: 1, blockers: [], next_actions: [], notifications: [{
		  id: notificationID, channel: "discord-main", status: "outcome_unknown", payload: {}, remote_receipt: {}, attempts: 1,
		  scheduled_at: "2026-08-08T00:00:00Z", created_at: "2026-08-08T00:00:00Z", updated_at: "2026-08-08T00:00:00Z",
		}]}
		: path === `/api/v2/notifications/${notificationID}/reconcile` && init?.method === "POST" ? {ok: true, status: "queued", notification_id: notificationID, notification: {id: notificationID, status: "queued"}, blockers: [], next_actions: []}
		: {};
	  return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
	});
	vi.stubGlobal("fetch", fetchMock);
	vi.spyOn(window, "prompt").mockReturnValue("a".repeat(64));
	vi.spyOn(window, "confirm").mockReturnValue(true);
	render(<App />);
	await userEvent.click(screen.getByRole("button", {name: "每日候选"}));
	await userEvent.click(await screen.findByRole("button", {name: "确认未送达并重试"}));
	await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => path === `/api/v2/notifications/${notificationID}/reconcile` && init?.method === "POST")).toBe(true));
	const call = fetchMock.mock.calls.find(([path, init]) => path === `/api/v2/notifications/${notificationID}/reconcile` && init?.method === "POST");
	expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({decision: "verified_not_delivered", confirmed: true, evidence_sha256: "a".repeat(64)});
  });

  it("requires reviewed legacy fingerprint before migration", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fingerprint = "a".repeat(64);
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
				: path === "/api/v2/adapters" ? {ok: true, status: "ready", catalog_version: "upload-assistant.adapter-catalog.v1", catalog_sha256: "a".repeat(64), count: 0, blockers: [], next_actions: [], adapters: []}
        : path === "/api/v2/downloaders" ? {downloaders: []}
				: path === "/api/v2/downloader-adapters" ? {adapters: [{adapter: "qbittorrent", display_name: "qBittorrent", runtime_supported: true, credential_fields: [], operations: {probe: true, add_torrent: true, inspect: true, list_torrents: true, list_files: true, set_limits: true, wait_complete: true, category: true, tags: true, skip_checking: true}}]}
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
