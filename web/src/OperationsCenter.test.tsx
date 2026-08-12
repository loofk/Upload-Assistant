import {afterEach,expect,it,vi} from "vitest";
import {cleanup,render,screen,waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OperationsCenter from "./OperationsCenter";
import {ApiClient} from "./api";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

it("shows safe provider failure evidence and full correlation IDs in logs", async () => {
	window.history.replaceState({}, "", "/app/operations/logs");
	const provider = {id: "22222222-2222-4222-8222-222222222222", name: "诊断模型", kind: "openai_compatible", base_url: "https://models.example.invalid/v1", model: "reasoner", data_level: "remote", api_mode: "chat_completions", reasoning_effort: "high", use_cases: ["incident_diagnosis"], json_mode: true, timeout_seconds: 60, enabled: true, outbound_consent: true, api_key_configured: true, health_status: "ready", capabilities: {catalog_source: "provider_models", models: []}, last_probe_evidence: {}};
	const log = {
		id: 42, occurred_at: "2026-08-10T07:14:43Z", level: "error", component: "http", message: "HTTP request",
		request_id: "dd33ce5e-12ae-4853-b465-55a4334a7995", trace_id: "ff4ceb73-9e05-4635-87ef-8b070f63341c",
		method: "POST", route: "/api/v2/site-rules/analyze", status_code: 504, duration_ms: 60006, error_code: "provider_timeout",
		attributes: {action: "site_rule_analysis", error_detail: "provider request timed out after 60 seconds", external_request: {endpoint_path: "/v1/chat/completions", body: {body_sha256: "a".repeat(64), preview: "{\"model\":\"reasoner\"}"}}, external_response: {status_code: 524, body: {body_sha256: "b".repeat(64), preview: "origin timed out"}}},
	};
	const fetchMock = vi.fn().mockImplementation((input:RequestInfo|URL, options?:RequestInit) => {
		const path=String(input);
		const body=path.includes("/operational-logs/42/context")?{context:{log,correlated_logs:[log],audit_events:[{id:"audit-1",actor_type:"user",action:"site_rule.ai_analyze_failed",resource_type:"site_rule_revision",payload:{error_code:"provider_timeout"},created_at:"2026-08-10T07:14:43Z"}]}}:path.includes("/llm-providers")?{llm_providers:[provider]}:path.endsWith("/api/v2/diagnostics")&&options?.method==="POST"?{diagnostic:{id:"diag-1",provider_id:provider.id,log_id:42,status:"queued",data_level:"remote",evidence_sha256:"c".repeat(64),created_at:"2026-08-10T07:15:00Z"}}:{operational_logs:[log]};
		return Promise.resolve(new Response(JSON.stringify(body),{status:path.endsWith("/api/v2/diagnostics")?202:200,headers:{"Content-Type":"application/json"}}));
	});
	vi.stubGlobal("fetch", fetchMock);
	render(<OperationsCenter client={new ApiClient("ua_test")} onError={(reason) => { throw reason; }} />);
	expect(await screen.findByText("provider request timed out after 60 seconds")).toBeInTheDocument();
	expect(screen.getByText("site_rule_analysis")).toBeInTheDocument();
	await userEvent.click(screen.getByRole("button",{name:"完整上下文"}));
	expect(await screen.findByText("外部请求参数（递归脱敏，超长截断）")).toBeInTheDocument();
	expect(screen.getAllByText(/origin timed out/).length).toBeGreaterThan(0);
	expect(screen.getByText("ff4ceb73-9e05-4635-87ef-8b070f63341c")).toBeInTheDocument();
	await userEvent.click(screen.getByRole("button",{name:"AI 分析"}));
	await waitFor(()=>expect(fetchMock.mock.calls.some(([value,options])=>String(value).endsWith("/api/v2/diagnostics")&&JSON.parse(String(options?.body)).log_id===42)).toBe(true));
});

it("keeps provider configuration out of operations diagnostics", async () => {
	window.history.replaceState({}, "", "/app/operations/diagnostics");
	vi.stubGlobal("fetch", vi.fn().mockImplementation((input:RequestInfo|URL)=>Promise.resolve(new Response(JSON.stringify(String(input).includes("/diagnostics?")?{diagnostics:[]}:{llm_providers:[]}),{status:200,headers:{"Content-Type":"application/json"}}))));
	render(<OperationsCenter client={new ApiClient("ua_test")} onError={(reason)=>{throw reason;}}/>);
	expect(await screen.findByText(/配置中心 \/ AI 模型/)).toBeInTheDocument();
	expect(screen.queryByRole("heading",{name:"模型服务"})).not.toBeInTheDocument();
	expect(screen.queryByRole("button",{name:"新增 Provider"})).not.toBeInTheDocument();
});

it("rejects a shortened trace prefix as an Incident ID before calling the API", async () => {
	window.history.replaceState({}, "", "/app/operations/diagnostics");
	const provider = {id: "22222222-2222-4222-8222-222222222222", name: "诊断模型", kind: "openai_compatible", base_url: "https://models.example.invalid/v1", model: "reasoner", data_level: "remote", api_mode: "chat_completions", reasoning_effort: "xhigh", use_cases: ["incident_diagnosis"], json_mode: true, timeout_seconds: 60, enabled: true, outbound_consent: true, api_key_configured: true, health_status: "ready", capabilities: {catalog_source: "provider_models", models: []}, last_probe_evidence: {}};
	const fetchMock = vi.fn().mockImplementation((input:RequestInfo|URL) => Promise.resolve(new Response(JSON.stringify(String(input).includes("/diagnostics?")?{diagnostics: []}:{llm_providers: [provider]}), {status: 200, headers: {"Content-Type": "application/json"}})));
	vi.stubGlobal("fetch", fetchMock);
	render(<OperationsCenter client={new ApiClient("ua_test")} onError={(reason) => { throw reason; }} />);
	await userEvent.type(await screen.findByLabelText("Incident ID"), "4201f48c");
	await userEvent.click(screen.getByRole("button", {name: "创建诊断"}));
	expect(await screen.findByRole("alert")).toHaveTextContent("必须是完整 UUID");
	expect(screen.getByRole("alert")).toHaveTextContent("8 位关联前缀不能作为诊断对象");
	expect(fetchMock.mock.calls.some(([value,options])=>String(value).endsWith("/api/v2/diagnostics")&&options?.method==="POST")).toBe(false);
});
