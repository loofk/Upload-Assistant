import {FormEvent, useCallback, useEffect, useState} from "react";
import type {ApiClient} from "./api";
import type {LLMProvider, ProviderUseCase} from "./types";
import {Drawer, InfoTip, ResourceHeader, SwitchField} from "./ui";

export default function LLMProvidersPanel({client, onError}: {client: ApiClient; onError: (reason: unknown) => void}) {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [editing, setEditing] = useState<LLMProvider | null | undefined>();
  const [busy, setBusy] = useState("");
  const [probeErrors, setProbeErrors] = useState<Record<string, {code: string; message: string}>>({});
  const load = useCallback(() => {
    void client.listLLMProviders().then(setProviders).catch(onError);
  }, [client, onError]);
  useEffect(load, [load]);

  const probe = async (provider: LLMProvider, stage: "catalog" | "inference") => {
    const key = `${provider.id}:${stage}`;
    setBusy(`probe:${key}`);
    setProbeErrors((current) => { const next = {...current}; delete next[key]; return next; });
    try {
      await client.probeLLMProvider(provider.id, stage);
    } catch (reason) {
      const value = reason as {code?: string; message?: string};
      setProbeErrors((current) => ({...current, [key]: {code: value.code ?? "provider_probe_failed", message: value.message ?? "Provider 探测失败"}}));
    } finally {
      setBusy("");
      load();
    }
  };

  return <section className="llm-configuration">
    <section className="provider-center">
      <ResourceHeader title="AI 模型服务" description="供站点规则分析和证据诊断使用。" action={<button className="primary" onClick={() => setEditing(null)}>新增模型</button>}/>
      {!providers.length ? <div className="provider-empty"><strong>还没有模型服务</strong><span>添加一个 OpenAI-compatible Provider 后，再显式发现模型和验证推理。</span></div> : <div className="provider-list">{providers.map((provider) => <article className="provider-item" key={provider.id}>
        <button className="provider-main" onClick={() => setEditing(provider)}><span className={`provider-health ${provider.health_status}`}/><span><strong>{provider.name}</strong><small>{provider.model} · {reasoningLabel(provider.reasoning_effort)}</small></span><i>{provider.enabled ? "已启用" : "已停用"}</i></button>
        <div className="provider-meta"><span>{provider.data_level === "local" ? "本地数据" : "远程脱敏数据"}</span>{provider.use_cases.map((value) => <span key={value}>{useCaseLabel(value)}</span>)}<span>{healthLabel(provider)}</span></div>
        <div className="provider-actions"><button className="secondary compact" onClick={() => setEditing(provider)}>编辑</button><button className="secondary compact" disabled={busy === `probe:${provider.id}:catalog`} onClick={() => void probe(provider, "catalog")}>{busy === `probe:${provider.id}:catalog` ? "发现中…" : "发现模型"}</button><button className="secondary compact" disabled={busy === `probe:${provider.id}:inference`} onClick={() => void probe(provider, "inference")}>{busy === `probe:${provider.id}:inference` ? "验证中…" : "验证调用契约"}</button></div>
        {(probeErrors[`${provider.id}:catalog`] || probeErrors[`${provider.id}:inference`]) && <div className="provider-probe-message" role="alert">{Object.entries(probeErrors).filter(([key]) => key.startsWith(`${provider.id}:`)).map(([key, value]) => <p key={key}><strong>{value.code}</strong>：{value.message}<span>{probeRecovery(value.code)}</span></p>)}</div>}
        <details className="provider-evidence"><summary>技术详情</summary><dl><div><dt>协议</dt><dd>{provider.api_mode === "responses" ? "Responses API" : "Chat Completions"} · {provider.streaming_enabled ? "SSE" : "整包"}</dd></div><div><dt>地址</dt><dd><code>{provider.base_url}</code></dd></div>{provider.last_probe_evidence?.performed_at && <><div><dt>最近探测</dt><dd>{provider.last_probe_evidence.stage} · HTTP {provider.last_probe_evidence.status_code ?? "—"}</dd></div><div><dt>摘要</dt><dd><code>{short(provider.last_probe_evidence.response_sha256 ?? "")}</code></dd></div></>}</dl></details>
      </article>)}</div>}
    </section>
    {editing !== undefined && <ProviderForm key={editing?.id ?? "new"} provider={editing ?? undefined} client={client} onCancel={() => setEditing(undefined)} onSaved={() => { setEditing(undefined); load(); }} onError={onError}/>}
  </section>;
}

function ProviderForm({provider, client, onSaved, onCancel, onError}: {provider?: LLMProvider; client: ApiClient; onSaved: () => void; onCancel: () => void; onError: (reason: unknown) => void}) {
  const [name, setName] = useState(provider?.name ?? "");
  const [baseURL, setBaseURL] = useState(provider?.base_url ?? "http://ollama:11434/v1");
  const [model, setModel] = useState(provider?.model ?? "");
  const [apiKey, setAPIKey] = useState("");
  const [dataLevel, setDataLevel] = useState<"local" | "remote">(provider?.data_level ?? "local");
  const [apiMode, setAPIMode] = useState<LLMProvider["api_mode"]>(provider?.api_mode ?? "chat_completions");
  const [reasoningEffort, setReasoningEffort] = useState<LLMProvider["reasoning_effort"]>(provider?.reasoning_effort ?? "default");
  const [useCases, setUseCases] = useState<ProviderUseCase[]>(provider?.use_cases ?? ["incident_diagnosis", "rule_analysis"]);
  const [timeoutSeconds, setTimeoutSeconds] = useState(provider?.timeout_seconds ?? 600);
  const [jsonMode, setJSONMode] = useState(provider?.json_mode ?? true);
  const [streamingEnabled, setStreamingEnabled] = useState(provider?.streaming_enabled ?? true);
  const [enabled, setEnabled] = useState(provider?.enabled ?? true);
  const [outboundConsent, setOutboundConsent] = useState(provider?.outbound_consent ?? false);
  const [custom, setCustom] = useState(() => Boolean(provider && (
    provider.api_mode !== "chat_completions" || provider.reasoning_effort !== "default" || provider.timeout_seconds !== 600 || provider.json_mode === false || provider.streaming_enabled === false
  )));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const toggleUseCase = (value: ProviderUseCase) => setUseCases((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  const reportedEfforts = provider?.capabilities?.models.find((item) => item.id === model)?.reasoning_efforts ?? [];
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      await client.putLLMProvider(provider?.id ?? crypto.randomUUID(), {name, baseURL, model, dataLevel, apiMode, reasoningEffort, useCases, jsonMode, streamingEnabled, timeoutSeconds, enabled, outboundConsent, apiKey});
      onSaved();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };
  const models = provider?.capabilities?.models ?? [];
  const modelOptions = model && !models.some((item) => item.id === model) ? [{id: model}, ...models] : models;
  const effortOptions = reportedEfforts.length ? ["default", ...reportedEfforts.filter((value) => value !== "default")] : ["default", "low", "medium", "high", "xhigh"];
  return <Drawer open title={provider ? `编辑模型 · ${provider.name}` : "新增 AI 模型"} description="保存不会联网；发现模型和验证调用均需单独点击。" dirty={dirty} onClose={onCancel}>
    <form className="provider-form config-drawer-form" onSubmit={(event) => void submit(event)} onChangeCapture={() => setDirty(true)}>
      <div className="provider-form-grid">
        <label>显示名称<input required value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 本地 Ollama"/></label>
        <label>数据边界<select value={dataLevel} onChange={(event) => setDataLevel(event.target.value as "local" | "remote")}><option value="local">本地服务</option><option value="remote">远程服务（仅脱敏数据）</option></select></label>
        <label className="span-2">服务地址<input required type="url" value={baseURL} onChange={(event) => setBaseURL(event.target.value)}/></label>
        <label>模型{modelOptions.length ? <select required value={model} onChange={(event) => setModel(event.target.value)}><option value="">请选择模型</option>{modelOptions.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}</select> : <input required value={model} onChange={(event) => setModel(event.target.value)} placeholder="模型 ID"/>}</label>
        <label>API Key<input type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setAPIKey(event.target.value)} placeholder={provider?.api_key_configured ? "留空保留现有密钥" : "可选"}/></label>
      </div>
      <fieldset className="provider-use-cases"><legend>使用场景</legend><label><input type="checkbox" checked={useCases.includes("incident_diagnosis")} onChange={() => toggleUseCase("incident_diagnosis")}/><span><strong>日志与异常诊断</strong><small>生成证据绑定的只读建议</small></span></label><label><input type="checkbox" checked={useCases.includes("rule_analysis")} onChange={() => toggleUseCase("rule_analysis")}/><span><strong>站点规则分析</strong><small>把规则原文整理为可审核配置</small></span></label></fieldset>
      <SwitchField checked={enabled} onChange={setEnabled} label="启用模型"/>
      {dataLevel === "remote" && <SwitchField checked={outboundConsent} onChange={setOutboundConsent} label="允许远程诊断" description="只发送递归脱敏后的证据。"/>}
      <SwitchField checked={custom} onChange={setCustom} label="自定义调用参数" description="默认使用流式 JSON 输出和 600 秒总预算。"/>
      {custom && <section className="provider-advanced"><label>API 协议<select value={apiMode} onChange={(event) => setAPIMode(event.target.value as LLMProvider["api_mode"])}><option value="chat_completions">Chat Completions</option><option value="responses">Responses API</option></select></label><label>推理强度<select value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value as LLMProvider["reasoning_effort"])}>{effortOptions.map((value) => <option key={value} value={value}>{value === "default" ? "模型默认" : value}</option>)}</select></label><label>超时<select value={timeoutSeconds} onChange={(event) => setTimeoutSeconds(Number(event.target.value))}><option value="120">120 秒</option><option value="300">300 秒</option><option value="600">600 秒</option></select></label><SwitchField checked={streamingEnabled} onChange={setStreamingEnabled} label="使用流式响应"/><SwitchField checked={jsonMode} onChange={setJSONMode} label="使用 JSON mode"/></section>}
      <footer><InfoTip label="模型安全说明">远程模型只接收脱敏证据；保存配置本身不会发起探测。</InfoTip><button className="primary" disabled={busy}>{busy ? "保存中…" : "保存配置"}</button></footer>
    </form>
  </Drawer>;
}

function reasoningLabel(value: LLMProvider["reasoning_effort"]) { return value === "default" ? "默认推理" : `${value} 推理`; }
function useCaseLabel(value: ProviderUseCase) { return value === "incident_diagnosis" ? "诊断" : "规则分析"; }
function healthLabel(provider: LLMProvider) { if (provider.health_status === "unknown") return "未测试"; if (provider.health_status === "catalog_ready") return `模型目录可达，调用契约未验证${provider.last_probe_latency_ms !== undefined ? ` · ${provider.last_probe_latency_ms} ms` : ""}`; if (provider.health_status === "failed") return `调用验证失败${provider.last_probe_error_code ? ` · ${provider.last_probe_error_code}` : ""}`; return `JSON / 流式调用契约已验证${provider.last_probe_latency_ms !== undefined ? ` · ${provider.last_probe_latency_ms} ms` : ""}`; }
function probeRecovery(code: string) { if (code === "provider_models_invalid") return "检查 Base URL 是否指向 OpenAI-compatible /v1，或改用 Provider 提供的兼容入口。"; if (code === "provider_model_unavailable") return "从已发现模型中选择精确模型 ID 后保存，再重新探测。"; if (code === "provider_http_error") return "检查 API Key、协议模式和上游访问策略。"; if (code === "provider_schema_invalid" || code === "provider_contract_invalid") return "推理响应不满足所选协议、JSON 模式或正常结束契约；切换 API 协议或修正兼容层后重试。"; if (code === "provider_output_truncated" || code === "provider_output_incomplete") return "模型没有正常完成有界测试输出；检查输出上限和兼容层 finish 状态。"; if (code === "provider_busy") return "当前分析容量已满，等待正在运行的调用结束后再验证。"; return "查看最近探测证据中的路径、HTTP 状态和响应结构后修正配置。"; }
function short(value: string) { return value ? value.slice(0, 8) : "—"; }
