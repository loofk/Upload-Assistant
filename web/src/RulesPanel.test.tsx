import {cleanup, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, expect, it, vi} from "vitest";
import {ApiClient} from "./api";
import RulesPanel from "./RulesPanel";
import type {AdapterCapability, AdapterCatalogEnvelope, SiteSummary} from "./types";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("orders enabled runtime sites first and hides sites without rule revisions by default", async () => {
  const sites: SiteSummary[] = [
    site("EMPTY", true, 0, "空站点"), site("MTEAM", false, 1, "馒头"), site("U2", true, 1, "U2 分享园"), site("CHD", true, 1, "彩虹岛"),
  ];
  const catalog: AdapterCatalogEnvelope = {
    ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 2, blockers: [], next_actions: [],
    adapters: [adapter("CHD"), adapter("U2")],
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = String(input);
    const payload = path.endsWith("/rules") ? {revisions: []} : path.endsWith("/credentials") ? {credentials: []} : path.endsWith("/llm-providers") ? {llm_providers: []} : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  }));
  const {container} = render(<RulesPanel sites={sites} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);
  await waitFor(() => expect(container.querySelectorAll(".site-directory > button")).toHaveLength(3));
  expect([...container.querySelectorAll(".site-directory > button")].map((item) => item.querySelector(".site-title strong")?.textContent)).toEqual(["彩虹岛", "U2 分享园", "馒头"]);
  expect(screen.getByRole("button", {name: /概览/})).toHaveAttribute("aria-current", "step");
  await openRuleStage("规则来源");
  expect(await screen.findByRole("group", {name: "规则来源是否完整"})).toBeInTheDocument();
  expect(screen.getByRole("radio", {name: "否"})).toBeChecked();
  expect(screen.getByLabelText("访问方式")).toHaveValue("none");
  expect(screen.getByText("规则页面无需登录")).toBeInTheDocument();
  expect(screen.queryByText("仅允许向这些站点发送 Cookie")).not.toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText("访问方式"), "site_cookie");
  expect(screen.getByText("仅允许向这些站点发送 Cookie")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("radio", {name: "是"}));
  expect(screen.getByRole("radio", {name: "是"})).toBeChecked();
  expect(screen.queryByText("空站点", {selector: ".site-title strong"})).not.toBeInTheDocument();
  await userEvent.click(screen.getByLabelText(/显示未配置站点/));
  expect(await screen.findByText("空站点", {selector: ".site-title strong"})).toBeInTheDocument();
});

it("explains a collection provider timeout after all rule pages were captured", async () => {
  const provider = {id: "22222222-2222-4222-8222-222222222222", name: "sub2api", kind: "openai_compatible", base_url: "https://models.example.invalid/v1", model: "gpt-5.6-sol", data_level: "remote", api_mode: "chat_completions", reasoning_effort: "high", use_cases: ["rule_analysis"], json_mode: true, streaming_enabled: true, timeout_seconds: 300, enabled: true, outbound_consent: true, api_key_configured: true, health_status: "ready", capabilities: {catalog_source: "provider_models", models: []}, last_probe_evidence: {}};
  const sourceSet = {site_code: "MTEAM", sources: [{id: "page-1", url: "https://wiki.example.invalid/rules", scope: "发布规则", auth_mode: "none"}], fingerprint: "a".repeat(64), scope_confirmed: true, cookie_hosts_confirmed: false, cookie_configured: false, cookie_required: false};
  const run = {id: "4549dd35-f790-4548-bcb9-9bd9833dfac8", site_code: "MTEAM", source_set_fingerprint: sourceSet.fingerprint, provider_id: provider.id, status: "failed", not_before: "2026-08-11T03:32:14Z", error_code: "provider_timeout", error_detail: "provider request timed out after 300 seconds", documents: [{id: "document", source_id: "page-1", url: sourceSet.sources[0].url, scope: "发布规则", auth_mode: "none", status: "ready", http_status: 200}], created_at: "2026-08-11T03:32:14Z", updated_at: "2026-08-11T03:37:37Z"};
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = String(input);
    const payload = path.endsWith("/rules") ? {revisions: []}
      : path.endsWith("/credentials") ? {credentials: []}
      : path.endsWith("/llm-providers") ? {llm_providers: [provider]}
      : path.endsWith("/access-policy") ? {access_policy: {site_code: "MTEAM", enabled: true, service_access: "allowed", search_access: "allowed", rule_schema_version: 2, active_requests: 0, general_used_this_hour: 0, search_used_this_hour: 0, blockers: [], operator_policy: {enabled: true, general_min_interval_seconds: 10, general_max_requests_per_hour: 120, search_min_interval_seconds: 30, search_max_requests_per_hour: 30, max_concurrency: 1}}}
      : path.endsWith("/rule-sources") ? {source_set: sourceSet}
      : path.endsWith("/rule-collection-runs/latest") ? {status: "failed", run}
      : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  }));
  const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("MTEAM")]};
  render(<RulesPanel sites={[site("MTEAM", true, 1, "馒头")]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);

  await openRuleStage("规则来源");
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("规则页面均已成功取证");
  expect(alert).toHaveTextContent("300 秒");
  expect(alert).toHaveTextContent("降低推理强度");
  expect(screen.getByRole("link", {name: "将模型超时调到 600 秒"})).toHaveAttribute("href", "/app/configuration/ai-models");
  expect(screen.getByText("high 推理 · 300 秒 · SSE 流式")).toBeInTheDocument();
});

it("renders hard gates and keeps advisories outside approval", async () => {
  const revision = {id: "11111111-1111-4111-8111-111111111111", site_id: "site", site_code: "CHD", revision: 1, status: "draft", fingerprint: "a".repeat(64), source_url: "https://example.test/rules", markdown_path: "CHD/rules.md", markdown_sha256: "b".repeat(64), policy: {}, obligations: [{id: "seedbox-limit", scope: "download", verification: "manual", blocking: true, resolution: "pending", description: "核对盒子单种上传限速", evidence_refs: ["规则页面截图"], enforcement: "上传前人工确认"}], created_at: "2026-08-09T00:00:00Z"};
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/markdown")) return Promise.resolve(new Response("# 原始规则", {status: 200, headers: {"Content-Type": "text/markdown"}}));
    const payload = path.endsWith("/rules") ? {revisions: [revision]}
      : path.endsWith("/credentials") ? {credentials: []}
      : path.endsWith("/llm-providers") ? {llm_providers: []}
      : path.endsWith("/review") ? {review: {revision_id: revision.id, site_code: "CHD", fingerprint: revision.fingerprint, revision_status: "draft", approval_ready: false, confirmed_count: 0, required_count: 2, blockers: [], next_actions: [], advisories: [{section: "seeding", severity: "warning", summary: "最低分享率需要留意"}], sections: [{key: "upload_limit", title: "上传限速硬门禁", status: "extracted", summary: "下载器执行限速", facts: [{label: "盒子上传上限", value: "20 MiB/s", detail: "任务执行时应用", tone: "positive"}], data: {seedbox_upload: "20MiB/s"}}]}} : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  }));
  const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("CHD")]};
  const {container} = render(<RulesPanel sites={[site("CHD", true, 1)]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);
  await openRuleStage("门禁审核");
  expect(await screen.findByText("20 MiB/s")).toBeInTheDocument();
  expect(screen.getByText("盒子上传上限")).toBeInTheDocument();
  expect(screen.getByText("任务执行时应用")).toBeInTheDocument();
  expect(container.querySelector(".advanced-rule-json")).not.toHaveAttribute("open");
  await userEvent.click(screen.getByRole("button", {name: "转种前提示 2"}));
  expect(screen.getByText("最低分享率需要留意")).toBeInTheDocument();
  expect(screen.getByText("核对盒子单种上传限速")).toBeInTheDocument();
  expect(screen.getByText("阻塞义务")).toBeInTheDocument();
  expect(screen.getByText("人工核对")).toBeInTheDocument();
  expect(screen.getByText("规则页面截图")).toBeInTheDocument();
  expect(screen.getByText("技术详情").closest("details")).not.toHaveAttribute("open");
});

it("renders legacy approved rules when optional review collections are null", async () => {
  const revision = {id: "12111111-1111-4111-8111-111111111111", site_id: "site", site_code: "CHD", revision: 1, status: "approved", fingerprint: "a".repeat(64), source_url: "https://example.test/rules", markdown_path: "CHD/rules.md", markdown_sha256: "b".repeat(64), policy: {}, obligations: null, created_at: "2026-08-09T00:00:00Z"};
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/markdown")) return Promise.resolve(new Response("# 原始规则", {status: 200, headers: {"Content-Type": "text/markdown"}}));
    const payload = path.endsWith("/rules") ? {revisions: [revision]}
      : path.endsWith("/credentials") ? {credentials: []}
      : path.endsWith("/llm-providers") ? {llm_providers: []}
      : path.endsWith("/review") ? {review: {revision_id: revision.id, site_code: "CHD", fingerprint: revision.fingerprint, revision_status: "approved", approval_ready: false, confirmed_count: 2, required_count: 2, blockers: null, next_actions: null, advisories: null, sections: [{key: "upload_limit", title: "上传限速硬门禁", status: "extracted", summary: "下载器执行限速", facts: [], data: {}}]}} : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  }));
  const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("CHD")]};
  render(<RulesPanel sites={[site("CHD", true, 1)]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);
  await openRuleStage("门禁审核");
  expect(await screen.findByText("上传限速硬门禁")).toBeInTheDocument();
	  expect(screen.getByText("变更已通过审核，等待应用")).toBeInTheDocument();
});

it("derives a new draft when an approved hard gate was missed instead of treating the review comment as configuration", async () => {
  const approved = {id: "12111111-1111-4111-8111-111111111111", site_id: "site", site_code: "CHD", revision: 3, status: "approved", fingerprint: "a".repeat(64), source_url: "https://example.test/rules", markdown_path: "CHD/r3.md", markdown_sha256: "b".repeat(64), policy: {limits: {}}, obligations: [], created_at: "2026-08-09T00:00:00Z"};
  const corrected = {...approved, id: "13111111-1111-4111-8111-111111111111", revision: 4, status: "draft", fingerprint: "c".repeat(64), markdown_path: "CHD/r4.md", policy: {limits: {upload: "100MB/s"}}};
  let correctionSaved = false;
  const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.includes("/corrections/upload_limit") && init?.method === "POST") {
      correctionSaved = true;
      return Promise.resolve(new Response(JSON.stringify({revision: corrected}), {status: 201, headers: {"Content-Type": "application/json"}}));
    }
    if (path.endsWith("/markdown")) return Promise.resolve(new Response("# 原始规则", {status: 200, headers: {"Content-Type": "text/markdown"}}));
    const selected = path.includes(corrected.id) ? corrected : approved;
    const payload = path.endsWith("/rules") ? {revisions: correctionSaved ? [corrected, approved] : [approved]}
      : path.endsWith("/credentials") ? {credentials: []}
      : path.endsWith("/llm-providers") ? {llm_providers: []}
      : path.endsWith("/review") ? {review: {revision_id: selected.id, site_code: "CHD", fingerprint: selected.fingerprint, revision_status: selected.status, approval_ready: false, confirmed_count: selected.status === "approved" ? 2 : 0, required_count: 2, blockers: [], next_actions: [], advisories: [], sections: [{key: "upload_limit", title: "上传限速硬门禁", status: "extracted", summary: "下载器执行限速", facts: [{label: "全局上传上限", value: selected.status === "approved" ? "未声明" : "100 MB/s"}], data: selected.status === "approved" ? {} : {upload: "100MB/s"}, check: selected.status === "approved" ? {section: "upload_limit", decision: "confirmed", comment: "{\n  \"upload\": \"100MB/s\"\n}", fingerprint: selected.fingerprint, reviewer_id: "reviewer", updated_at: "2026-08-10T00:00:00Z"} : undefined}]}} : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  });
  vi.stubGlobal("fetch", fetchMock);
  const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("CHD")]};
  render(<RulesPanel sites={[site("CHD", true, 1)]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);

  await openRuleStage("门禁审核");
  expect(await screen.findByText("未声明")).toBeInTheDocument();
  expect(screen.queryByText("审核备注")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", {name: "调整门禁"}));
  await userEvent.type(screen.getByLabelText("站点声明上传上限"), "120MB/s");
  await userEvent.type(screen.getByLabelText("实际单种上传上限"), "100MB/s");
  await userEvent.type(screen.getByLabelText("调整依据（必填）"), "原文明确要求 100MB/s，AI 漏识别");
  await userEvent.click(screen.getByRole("button", {name: "保存为新 revision"}));

	  expect(await screen.findByRole("button", {name: /待审核变更.*r4/})).toBeInTheDocument();
  const correctionCall = fetchMock.mock.calls.find(([value, options]) => String(value).includes("/corrections/upload_limit") && options?.method === "POST");
  expect(JSON.parse(String(correctionCall?.[1]?.body))).toEqual({fingerprint: approved.fingerprint, data: {
    upload: "100MB/s", upload_declared: "120MB/s", upload_safety_margin: "20MB/s", upload_scope: "per_torrent",
    seedbox_upload: "", seedbox_upload_declared: "", seedbox_upload_safety_margin: "20MB/s", seedbox_upload_scope: "per_torrent",
  }, comment: "原文明确要求 100MB/s，AI 漏识别"});
  expect(fetchMock.mock.calls.some(([value]) => String(value).endsWith("/activate"))).toBe(false);
});

it("shows one baseline and one pending change while keeping older revisions in audit history", async () => {
	const baseline = {id: "61111111-1111-4111-8111-111111111111", site_id: "site", site_code: "CHD", revision: 3, status: "approved", fingerprint: "b".repeat(64), source_url: "https://example.test/rules", markdown_path: "CHD/r3.md", markdown_sha256: "b".repeat(64), policy: {limits: {upload: "80MB/s", download: "100MB/s"}, naming: {profiles: [{category: "movie"}]}}, obligations: [], created_at: "2026-08-09T00:00:00Z"};
	const pending = {...baseline, id: "71111111-1111-4111-8111-111111111111", revision: 4, status: "draft", fingerprint: "c".repeat(64), markdown_path: "CHD/r4.md", policy: {limits: {upload: "60MB/s", download: "100MB/s"}, naming: {profiles: [{category: "movie"}]}}};
	const oldApproved = {...baseline, id: "51111111-1111-4111-8111-111111111111", revision: 2, status: "retired", fingerprint: "d".repeat(64), markdown_path: "CHD/r2.md"};
	const oldDraft = {...baseline, id: "41111111-1111-4111-8111-111111111111", revision: 1, status: "retired", fingerprint: "e".repeat(64), markdown_path: "CHD/r1.md"};
	let discarded = false;
	const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
		const path = String(input);
		if (path.endsWith(`/${pending.id}/discard`) && init?.method === "POST") { discarded = true; return Promise.resolve(new Response(JSON.stringify({revision: {...pending, status: "retired"}}), {status: 200, headers: {"Content-Type": "application/json"}})); }
		if (path.endsWith("/markdown")) return Promise.resolve(new Response("# 原始规则", {status: 200, headers: {"Content-Type": "text/markdown"}}));
		const selected = path.includes(pending.id) ? pending : baseline;
		const payload = path.endsWith("/rules") ? {revisions: discarded ? [{...pending, status: "retired"}, baseline, oldApproved, oldDraft] : [pending, baseline, oldApproved, oldDraft]}
			: path.endsWith("/credentials") ? {credentials: []}
			: path.endsWith("/llm-providers") ? {llm_providers: []}
			: path.endsWith("/review") ? {review: {revision_id: selected.id, site_code: "CHD", fingerprint: selected.fingerprint, revision_status: selected.status, approval_ready: false, confirmed_count: 0, required_count: 3, blockers: [], next_actions: [], advisories: [], sections: []}} : {};
		return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
	});
	vi.stubGlobal("fetch", fetchMock);
	vi.spyOn(window, "confirm").mockReturnValue(true);
	const configuredSite = {...site("CHD", true, 2), active_rule_revision_id: baseline.id, active_rule_fingerprint: baseline.fingerprint};
	const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("CHD")]};
	const {container} = render(<RulesPanel sites={[configuredSite]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);

	await openRuleStage("门禁审核");
	await screen.findByText("审核的是这次变化；应用后，它会成为唯一的运行时基准。");
	expect(container.querySelectorAll(".baseline-switch > button")).toHaveLength(2);
	expect(screen.getByRole("button", {name: /当前基准.*r3/})).toBeInTheDocument();
	expect(screen.getByRole("button", {name: /待审核变更.*r4/})).toBeInTheDocument();
	expect(screen.getByText("历史记录（2）")).toBeInTheDocument();
	expect(screen.getByText("上传限速").closest("span")).toHaveTextContent("有变化");
	expect(screen.getByText("下载限速").closest("span")).toHaveTextContent("未变化");

	await userEvent.click(await screen.findByRole("button", {name: "放弃变更"}));
	await waitFor(() => expect(fetchMock.mock.calls.some(([value, options]) => String(value).endsWith(`/${pending.id}/discard`) && options?.method === "POST")).toBe(true));
	expect(await screen.findByText("当前没有待审核或待应用的规则变更。")).toBeInTheDocument();
	expect(container.querySelectorAll(".baseline-switch > button")).toHaveLength(1);
});

it("uses one stable per-site access policy and adapter-driven credential fields", async () => {
  const sourceSet = {site_code: "CHD", sources: [{id: "page-1", url: "https://rules.example.invalid/rules", scope: "全部规则", auth_mode: "none"}], fingerprint: "a".repeat(64), scope_confirmed: true, cookie_hosts_confirmed: false, cookie_configured: false, cookie_required: false};
  const accessPolicy = {
    site_code: "CHD", enabled: true, service_access: "undetermined", search_access: "undetermined", rule_schema_version: 0,
    general_min_interval_seconds: 10, general_max_requests_per_hour: 120, search_min_interval_seconds: 30, search_max_requests_per_hour: 30, max_concurrency: 1,
    active_requests: 0, general_used_this_hour: 7, search_used_this_hour: 2,
    blockers: [
      {code: "site_service_access_forbidden", message: "活动规则未明确允许服务访问该站点"},
      {code: "site_search_access_forbidden", message: "活动规则未明确允许服务搜索该站点"},
    ],
    operator_policy: {enabled: true, general_min_interval_seconds: 10, general_max_requests_per_hour: 120, search_min_interval_seconds: 30, search_max_requests_per_hour: 30, max_concurrency: 1},
  };
  const savedCredentials = [{id: "credential-cookie", site_code: "CHD", name: "cookie", enabled: true, created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z"}];
  let passkeySaved = false;
  const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/credentials/passkey") && init?.method === "PUT") {
      passkeySaved = true;
      return Promise.resolve(new Response(JSON.stringify({ok: true}), {status: 200, headers: {"Content-Type": "application/json"}}));
    }
    const payload = path.endsWith("/rules") ? {revisions: []}
      : path.endsWith("/credentials") ? {credentials: passkeySaved ? [...savedCredentials, {id: "credential-passkey", site_code: "CHD", name: "passkey", enabled: true, created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z"}] : savedCredentials}
      : path.endsWith("/llm-providers") ? {llm_providers: []}
      : path.endsWith("/access-policy") ? {access_policy: accessPolicy}
      : path.endsWith("/rule-sources") ? {source_set: sourceSet}
      : path.endsWith("/rule-collection-runs/latest") ? {status: "not_found"}
      : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  });
  vi.stubGlobal("fetch", fetchMock);
  const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("CHD", ["cookie", "passkey"])]};
  const {container} = render(<RulesPanel sites={[site("CHD", true, 1, "彩虹岛")]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);

  await openRuleStage("运行配置");
  const accessEditor = await screen.findByRole("region", {name: "站点访问频率"});
  expect(within(accessEditor).getByText("已启用")).toBeInTheDocument();
  expect(screen.queryByText("仅规则采集可用")).not.toBeInTheDocument();
  expect(screen.queryByText("规则采集配置")).not.toBeInTheDocument();
  expect(screen.queryByText("当前禁止服务访问")).not.toBeInTheDocument();
  expect(screen.queryByText("活动规则未明确允许服务访问该站点")).not.toBeInTheDocument();
  expect(screen.queryByText("活动规则未明确允许服务搜索该站点")).not.toBeInTheDocument();
  expect(screen.queryByText("高级：手动粘贴或重新分析")).not.toBeInTheDocument();
  expect(screen.queryByText("审批备注")).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name: "站点访问频率说明"})).toHaveAttribute("aria-describedby", "access-policy-help-CHD");
  expect(screen.getByRole("tooltip", {name: /当前站点唯一的访问频率配置/})).toHaveTextContent("规则采集、站点搜索和转种流程共用");
  expect(within(accessEditor).getByRole("checkbox", {name: /启用站点访问/})).toBeChecked();
  expect(within(accessEditor).queryByLabelText("普通请求最小间隔（秒）")).not.toBeInTheDocument();
  await userEvent.click(within(accessEditor).getByRole("button", {name: "调整频率"}));
  expect(within(accessEditor).getByLabelText("普通请求最小间隔（秒）")).toHaveValue(10);
  expect(within(accessEditor).getByLabelText("普通请求每小时上限")).toHaveValue(120);
  expect(within(accessEditor).getByLabelText("搜索最小间隔（秒）")).toHaveValue(30);
  expect(within(accessEditor).getByLabelText("搜索每小时上限")).toHaveValue(30);
  expect(within(accessEditor).getByLabelText("站点并发上限")).toHaveValue(1);

  await userEvent.click(within(accessEditor).getByRole("checkbox", {name: /启用站点访问/}));
  expect(within(accessEditor).getByText("已启用")).toBeInTheDocument();
  for (const input of within(accessEditor).getAllByRole("spinbutton")) expect(input).toBeDisabled();
  expect(container.querySelectorAll(".rule-runtime-stage > .rule-side-card")).toHaveLength(2);
  expect(container.querySelector(".rule-runtime-stage > details")).toBeNull();

  const credentialEditor = screen.getByRole("region", {name: "站点凭据"});
  expect(within(credentialEditor).getByText("Cookie + Passkey")).toBeInTheDocument();
  expect(within(credentialEditor).getByLabelText("Cookie")).toHaveAttribute("placeholder", "输入新值可更新");
  expect(within(credentialEditor).getByLabelText("Passkey")).toHaveAttribute("placeholder", "输入 Passkey");
  const syntheticSecret = "synthetic-passkey-value";
  await userEvent.type(within(credentialEditor).getByLabelText("Passkey"), syntheticSecret);
  await userEvent.click(within(credentialEditor).getByRole("button", {name: "保存"}));
  await waitFor(() => expect(passkeySaved).toBe(true));
  expect(JSON.parse(String(fetchMock.mock.calls.find(([value, options]) => String(value).endsWith("/credentials/passkey") && options?.method === "PUT")?.[1]?.body))).toEqual({value: syntheticSecret});
  await waitFor(() => expect(within(credentialEditor).getAllByText("已加密保存", {selector: ".credential-state"})).toHaveLength(2));
  expect(within(credentialEditor).getByLabelText("Passkey")).toHaveValue("");
  expect(screen.queryByText(syntheticSecret)).not.toBeInTheDocument();
});

it("renders only the MTEAM API key required by its adapter", async () => {
  const sourceSet = {site_code: "MTEAM", sources: [{id: "page-1", url: "https://wiki.m-team.cc/zh-tw/upload-rules", scope: "发布规则", auth_mode: "none"}], fingerprint: "b".repeat(64), scope_confirmed: true, cookie_hosts_confirmed: false, cookie_configured: false, cookie_required: false};
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = String(input);
    const payload = path.endsWith("/rules") ? {revisions: []}
      : path.endsWith("/credentials") ? {credentials: []}
      : path.endsWith("/llm-providers") ? {llm_providers: []}
      : path.endsWith("/access-policy") ? {access_policy: {site_code: "MTEAM", enabled: false, service_access: "undetermined", search_access: "undetermined", rule_schema_version: 0, active_requests: 0, general_used_this_hour: 0, search_used_this_hour: 0, blockers: [], operator_policy: {enabled: false, general_min_interval_seconds: 10, general_max_requests_per_hour: 120, search_min_interval_seconds: 30, search_max_requests_per_hour: 30, max_concurrency: 1}}}
      : path.endsWith("/rule-sources") ? {source_set: sourceSet}
      : path.endsWith("/rule-collection-runs/latest") ? {status: "not_found"}
      : {};
    return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
  }));
  const catalog: AdapterCatalogEnvelope = {ok: true, status: "ready", catalog_version: "v1", catalog_sha256: "a".repeat(64), count: 1, blockers: [], next_actions: [], adapters: [adapter("MTEAM", ["api_key"], "mteam_api")]};
  render(<RulesPanel sites={[site("MTEAM", true, 1, "馒头")]} catalog={catalog} client={new ApiClient("ua_test")} reloadSites={async () => {}} onError={(reason) => { throw reason; }} />);

  await openRuleStage("运行配置");
  const credentialEditor = await screen.findByRole("region", {name: "站点凭据"});
  expect(within(credentialEditor).getByText("API Key", {selector: ".credential-profile"})).toBeInTheDocument();
  expect(within(credentialEditor).getByLabelText("API Key")).toBeInTheDocument();
  expect(within(credentialEditor).queryByLabelText("Cookie")).not.toBeInTheDocument();
  expect(within(credentialEditor).queryByLabelText("Passkey")).not.toBeInTheDocument();
});

function site(code: string, enabled: boolean, ruleRevisionCount: number, name = code): SiteSummary {
  return {id: code, code, name, adapter: "nexusphp", enabled, live_validation_status: "unverified", rule_revision_count: ruleRevisionCount, aliases: [], tags: []};
}

function adapter(siteCode: string, credentialFields: string[] = [], adapterName = "nexusphp"): AdapterCapability {
  return {id: `site/${siteCode}`, kind: "site", adapter: adapterName, display_name: siteCode, site_code: siteCode, runtime_supported: true, operations: [], credential_fields: credentialFields, safety_gates: [], constraints: []};
}

async function openRuleStage(name: "规则来源" | "门禁审核" | "运行配置") {
  const navigation = await screen.findByRole("navigation", {name: "站点规则阶段"});
  await userEvent.click(within(navigation).getByRole("button", {name: new RegExp(name)}));
}
