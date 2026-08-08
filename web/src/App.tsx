import {FormEvent, useCallback, useEffect, useMemo, useState} from "react";
import {ApiClient, ApiError} from "./api";
import Candidates from "./Candidates";
import Configuration from "./Configuration";
import Audit from "./Audit";
import Readiness from "./Readiness";
import type {
  Artifact,
  AuditEvent,
  Blocker,
  CreateJobInput,
  Job,
  JobStatus,
  JobSummaryEnvelope,
  JsonValue,
  Step,
} from "./types";

const tokenKey = "ua.v2.api-token";
const activeStatuses = new Set<JobStatus>(["queued", "running"]);
const terminalStatuses = new Set<JobStatus>(["complete", "cancelled"]);
const downloadableArtifactKinds = new Set([
  "content_manifest", "metadata", "mediainfo", "bdinfo", "screenshot", "image_upload_receipt",
  "target_package", "duplicate_check", "target_torrent_receipt", "preupload_duplicate_check",
  "target_upload_receipt", "target_torrent_download_receipt", "target_injection_receipt",
  "target_seed_observation", "job_summary",
  "candidate_scan", "candidate_evaluation", "candidate_digest", "candidate_summary",
]);
const statusLabels: Record<JobStatus, string> = {
  draft: "草稿",
  queued: "排队中",
  running: "执行中",
  paused: "已暂停",
  blocked: "被阻塞",
  failed: "失败",
  complete: "已完成",
  cancelled: "已取消",
};

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem(tokenKey) ?? "");
  if (!token) {
    return <ConnectScreen onConnect={(value) => {
      sessionStorage.setItem(tokenKey, value);
      setToken(value);
    }} />;
  }
  return <Console token={token} onDisconnect={() => {
    sessionStorage.removeItem(tokenKey);
    setToken("");
  }} />;
}

function ConnectScreen({onConnect}: {onConnect: (token: string) => void}) {
  const [value, setValue] = useState("");
  return (
    <main className="connect-shell">
      <section className="connect-card">
        <div className="brand-mark" aria-hidden="true">UA</div>
        <p className="eyebrow">LOCAL CONTROL PLANE</p>
        <h1>转种工作台</h1>
        <p className="connect-copy">查看每一个持久化步骤、规则门禁、证据文件与做种状态。服务默认仅监听本机，所有操作仍由 API 权限控制。</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          const token = value.trim();
          if (token) onConnect(token);
        }}>
          <label htmlFor="api-token">API Token</label>
          <input
            id="api-token"
            type="password"
            autoComplete="off"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="ua_…"
            minLength={32}
            required
          />
          <button className="primary wide" type="submit">进入控制台</button>
        </form>
        <p className="security-note">Token 只保存在当前浏览器标签会话的 sessionStorage，关闭标签后失效。</p>
      </section>
    </main>
  );
}

function Console({token, onDisconnect}: {token: string; onDisconnect: () => void}) {
  const client = useMemo(() => new ApiClient(token), [token]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedID, setSelectedID] = useState("");
  const [detail, setDetail] = useState<JobSummaryEnvelope | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [statusFilter, setStatusFilter] = useState<JobStatus | "">("");
  const [nextCursor, setNextCursor] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [section, setSection] = useState<"jobs" | "candidates" | "configuration" | "readiness" | "audit">("jobs");

  const describeError = useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 401) return "API Token 无效、已撤销或已过期。";
    return reason instanceof Error ? reason.message : "请求失败，请稍后重试。";
  }, []);

  const loadJobs = useCallback(async (cursor = "", append = false) => {
    setLoading(true);
    try {
      const page = await client.listJobs({status: statusFilter, cursor, limit: 25});
      setJobs((current) => append ? [...current, ...page.jobs] : page.jobs);
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
      setError("");
      if (!append && !selectedID && page.jobs.length > 0) setSelectedID(page.jobs[0].id);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, [client, describeError, selectedID, statusFilter]);

  const loadDetail = useCallback(async (jobID: string) => {
    if (!jobID) return;
    try {
      const [summary, audit] = await Promise.all([client.getSummary(jobID), client.getEvents(jobID)]);
      setDetail(summary);
      setEvents(audit.events);
      setError("");
    } catch (reason) {
      setError(describeError(reason));
    }
  }, [client, describeError]);

  useEffect(() => {
    void loadJobs();
  }, [statusFilter]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setDetail(null);
    setEvents([]);
    void loadDetail(selectedID);
  }, [selectedID, loadDetail]);

  useEffect(() => {
    if (!selectedID || !detail || !activeStatuses.has(detail.status)) return;
    const timer = window.setInterval(() => {
      void loadDetail(selectedID);
      void loadJobs();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [detail?.status, loadDetail, loadJobs, selectedID]);

  const refreshAll = async () => {
    await Promise.all([loadJobs(), selectedID ? loadDetail(selectedID) : Promise.resolve()]);
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark small" aria-hidden="true">UA</div>
          <div><strong>Upload Assistant</strong><span>可审计转种控制台</span></div>
        </div>
        <nav className="main-nav" aria-label="主导航"><button className={section === "jobs" ? "active" : ""} onClick={() => setSection("jobs")}>任务</button><button className={section === "candidates" ? "active" : ""} onClick={() => setSection("candidates")}>每日候选</button><button className={section === "configuration" ? "active" : ""} onClick={() => setSection("configuration")}>配置</button><button className={section === "readiness" ? "active" : ""} onClick={() => setSection("readiness")}>就绪检查</button><button className={section === "audit" ? "active" : ""} onClick={() => setSection("audit")}>审计</button></nav>
        <div className="topbar-actions">
          <span className="service-state"><i /> 本地服务已连接</span>
          <button className="ghost" onClick={() => void refreshAll()}>刷新</button>
          <button className="ghost" onClick={onDisconnect}>退出会话</button>
        </div>
      </header>

      {error && <div className="global-error" role="alert"><span>{error}</span><button onClick={() => setError("")}>关闭</button></div>}

      {section === "audit" ? <Audit client={client} onError={(reason) => setError(describeError(reason))} /> : section === "readiness" ? <Readiness client={client} onError={(reason) => setError(describeError(reason))} /> : section === "configuration" ? <Configuration client={client} onError={(reason) => setError(describeError(reason))} /> : section === "candidates" ? <Candidates client={client} onError={(reason) => setError(describeError(reason))} onJobCreated={(jobID) => { setSection("jobs"); setSelectedID(jobID); void loadJobs(); }} /> : <div className="workspace">
        <aside className="job-sidebar">
          <div className="sidebar-heading">
            <div><p className="eyebrow">DURABLE JOBS</p><h2>任务</h2></div>
            <button className="primary compact" onClick={() => setCreateOpen(true)}>新建</button>
          </div>
          <label className="filter-label">
            状态筛选
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as JobStatus | "")}>
              <option value="">全部状态</option>
              {Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
          <div className="job-list" aria-busy={loading}>
            {jobs.map((job) => <JobCard key={job.id} job={job} selected={job.id === selectedID} onSelect={setSelectedID} />)}
            {!loading && jobs.length === 0 && <div className="empty compact-empty">当前筛选条件下没有任务。</div>}
            {loading && jobs.length === 0 && <div className="skeleton-list"><i /><i /><i /></div>}
          </div>
          {hasMore && <button className="load-more" onClick={() => void loadJobs(nextCursor, true)}>加载更早任务</button>}
        </aside>

        <main className="detail-pane">
          {selectedID ? (
            detail ? <JobDetail
              detail={detail}
              events={events}
              client={client}
              onChanged={refreshAll}
              onError={(reason) => setError(describeError(reason))}
            /> : <DetailSkeleton />
          ) : <WelcomePanel onCreate={() => setCreateOpen(true)} />}
        </main>
      </div>}

      {createOpen && <CreateJobDialog
        client={client}
        onClose={() => setCreateOpen(false)}
        onCreated={(jobID) => {
          setCreateOpen(false);
          setSelectedID(jobID);
          void loadJobs();
        }}
        onError={(reason) => setError(describeError(reason))}
      />}
    </div>
  );
}

function JobCard({job, selected, onSelect}: {job: Job; selected: boolean; onSelect: (id: string) => void}) {
  const source = typeof job.input?.source_url === "string" ? job.input.source_url : typeof job.input?.source === "string" ? job.input.source : "来源待解析";
  const target = typeof job.input?.target === "string" ? job.input.target : "目标待解析";
  let sourceLabel = source;
  try { sourceLabel = new URL(source).hostname; } catch { /* Keep the bounded redacted string. */ }
  return (
    <button className={`job-card ${selected ? "selected" : ""}`} onClick={() => onSelect(job.id)}>
      <div className="job-card-row"><StatusPill status={job.status} /><time>{formatDate(job.created_at, true)}</time></div>
      <strong>{sourceLabel} <span>→</span> {target}</strong>
      <small>{job.current_step || "流程结束"} · {shortID(job.id)}</small>
    </button>
  );
}

function JobDetail({
  detail, events, client, onChanged, onError,
}: {
  detail: JobSummaryEnvelope;
  events: AuditEvent[];
  client: ApiClient;
  onChanged: () => Promise<void>;
  onError: (reason: unknown) => void;
}) {
  const [tab, setTab] = useState<"steps" | "artifacts" | "events" | "summary">("steps");
  const [resumeText, setResumeText] = useState("{}");
  const [confirmUpload, setConfirmUpload] = useState(false);
  const [busy, setBusy] = useState(false);
  const confirmRequired = detail.blockers.some((blocker) => blocker.code === "confirm_upload_required");

  useEffect(() => {
    setResumeText(JSON.stringify(detail.resume_state ?? {}, null, 2));
    setConfirmUpload(false);
  }, [detail.job_id, detail.status, detail.current_step, detail.resume_state]);

  const transition = async (action: "pause" | "cancel") => {
    if (action === "cancel" && !window.confirm("确认取消这个任务？持久化证据和已下载数据不会被删除。")) return;
    setBusy(true);
    try {
      await client.transition(detail.job_id, action);
      await onChanged();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const resume = async () => {
    if (confirmRequired && !confirmUpload) {
      onError(new Error("live 上传需要先勾选显式确认。"));
      return;
    }
    let state: Record<string, JsonValue>;
    try {
      const parsed: unknown = JSON.parse(resumeText);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error();
      state = parsed as Record<string, JsonValue>;
    } catch {
      onError(new Error("resume_state 必须是合法 JSON 对象。"));
      return;
    }
    if (confirmRequired) state.confirm_upload = true;
    setBusy(true);
    try {
      await client.resume(detail.job_id, state);
      await onChanged();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const canPause = ["queued", "running", "blocked", "failed"].includes(detail.status);
  const canResume = ["paused", "blocked", "failed"].includes(detail.status);
  const completedSteps = detail.steps.filter((step) => step.status === "complete" || step.status === "skipped").length;
  const progress = detail.steps.length ? Math.round((completedSteps / detail.steps.length) * 100) : 0;

  return (
    <article className="job-detail">
      <header className="detail-header">
        <div>
          <div className="title-row"><StatusPill status={detail.status} /><span className="mode-label">{detail.current_step || "流程结束"}</span></div>
          <h1>{detail.kind === "daily_candidates" ? "每日候选任务" : "转种任务"} <span>{shortID(detail.job_id)}</span></h1>
          <button className="copy-id" onClick={() => void navigator.clipboard?.writeText(detail.job_id)}>{detail.job_id} · 复制</button>
        </div>
        <div className="detail-actions">
          {canPause && <button className="secondary" disabled={busy} onClick={() => void transition("pause")}>暂停</button>}
          {canResume && <button className="primary" disabled={busy} onClick={() => void resume()}>续跑</button>}
          {!terminalStatuses.has(detail.status) && <button className="danger" disabled={busy} onClick={() => void transition("cancel")}>取消</button>}
        </div>
      </header>

      <section className="progress-band">
        <div><strong>{completedSteps} / {detail.steps.length}</strong><span>步骤完成</span></div>
        <div className="progress-track"><i style={{width: `${progress}%`}} /></div>
        <b>{progress}%</b>
      </section>

      {detail.blockers.length > 0 && <BlockerPanel blockers={detail.blockers} />}

      {canResume && <section className="resume-panel">
        <div className="section-title"><div><p className="eyebrow">RECOVERY INPUT</p><h2>恢复参数</h2></div><span>提交后会写入审计事件</span></div>
        <textarea aria-label="resume_state JSON" spellCheck={false} value={resumeText} onChange={(event) => setResumeText(event.target.value)} />
        {confirmRequired && <label className="confirm-live">
          <input type="checkbox" checked={confirmUpload} onChange={(event) => setConfirmUpload(event.target.checked)} />
          <span><strong>我已人工复核目标站规则、最终查重与不可变上传包，并确认执行 live 上传。</strong><small>此确认不会被系统或 AI 自动推断。</small></span>
        </label>}
        <button className="primary" disabled={busy || (confirmRequired && !confirmUpload)} onClick={() => void resume()}>写入参数并续跑</button>
      </section>}

      <nav className="tabs" aria-label="任务详情">
        {(["steps", "artifacts", "events", "summary"] as const).map((value) => (
          <button key={value} className={tab === value ? "active" : ""} onClick={() => setTab(value)}>
            {value === "steps" ? `步骤 ${detail.steps.length}` : value === "artifacts" ? `证据 ${detail.artifacts.length}` : value === "events" ? `日志 ${events.length}` : "总结"}
          </button>
        ))}
      </nav>

      {tab === "steps" && <StepsView steps={detail.steps} />}
      {tab === "artifacts" && <ArtifactsView artifacts={detail.artifacts} jobID={detail.job_id} client={client} onError={onError} />}
      {tab === "events" && <EventsView events={events} />}
      {tab === "summary" && <SummaryView summary={detail.summary} status={detail.status} />}
    </article>
  );
}

function BlockerPanel({blockers}: {blockers: Blocker[]}) {
  return <section className="blocker-panel"><div className="blocker-icon">!</div><div><p className="eyebrow">HARD GATE</p><h2>任务需要人工处理</h2>{blockers.map((blocker) => (
    <div className="blocker-row" key={`${blocker.code}-${blocker.site_code ?? ""}`}><code>{blocker.code}</code><span>{blocker.message || "未提供说明"}</span>{blocker.site_code && <b>{blocker.site_code}</b>}</div>
  ))}</div></section>;
}

function StepsView({steps}: {steps: Step[]}) {
  return <section className="step-list">{steps.map((step) => (
    <details className={`step-row status-${step.status}`} key={step.id} open={step.status === "blocked" || step.status === "failed"}>
      <summary>
        <span className="step-index">{String(step.position).padStart(2, "0")}</span>
        <span className="step-state-dot" />
        <div><strong>{humanizeStep(step.key)}</strong><small>{step.key}{step.gate_kind ? ` · gate: ${step.gate_kind}` : ""}</small></div>
        <StatusPill status={step.status as JobStatus} />
      </summary>
      <div className="step-evidence">
        <div className="snapshot-proof"><span>输入快照已隐藏</span><code>sha256:{shortHash(step.input_snapshot?.sha256)}</code></div>
        {step.blockers?.length > 0 && <BlockerPanel blockers={step.blockers} />}
        <JsonBlock value={step.output_summary} emptyLabel="此步骤还没有输出证据。" />
      </div>
    </details>
  ))}</section>;
}

function ArtifactsView({artifacts, jobID, client, onError}: {artifacts: Artifact[]; jobID: string; client: ApiClient; onError: (reason: unknown) => void}) {
  const [downloading, setDownloading] = useState("");
  const download = async (artifact: Artifact) => {
    setDownloading(artifact.id);
    try {
      const blob = await client.downloadArtifact(jobID, artifact.id);
      const objectURL = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectURL;
      anchor.download = artifact.filename;
      anchor.click();
      URL.revokeObjectURL(objectURL);
    } catch (reason) {
      onError(reason);
    } finally {
      setDownloading("");
    }
  };
  if (!artifacts.length) return <Empty text="尚未登记 artifact。每个外部边界完成后都会在这里留下 SHA-256 证据。" />;
  return <section className="table-wrap"><table><thead><tr><th>类型 / 文件</th><th>大小</th><th>SHA-256</th><th>登记时间</th><th>内容</th></tr></thead><tbody>{artifacts.map((artifact) => (
    <tr key={artifact.id}><td><strong>{artifact.kind}</strong><small>{artifact.filename}</small></td><td>{formatBytes(artifact.size_bytes)}</td><td><code title={artifact.sha256}>{shortHash(artifact.sha256)}</code></td><td>{formatDate(artifact.created_at)}</td><td>{downloadableArtifactKinds.has(artifact.kind) ? <button className="artifact-download" disabled={downloading === artifact.id} onClick={() => void download(artifact)}>{downloading === artifact.id ? "校验中…" : "校验并下载"}</button> : <span className="restricted-content">敏感内容受限</span>}</td></tr>
  ))}</tbody></table></section>;
}

function EventsView({events}: {events: AuditEvent[]}) {
  if (!events.length) return <Empty text="尚无审计事件。" />;
  return <section className="event-list">{events.map((event) => (
    <details className="event-row" key={event.id}>
      <summary><span className="event-sequence">#{event.sequence}</span><div><strong>{event.type}</strong><small>{event.actor_type}{event.actor_id ? ` · ${event.actor_id}` : ""}</small></div><time>{formatDate(event.created_at)}</time><code>{shortHash(event.hash)}</code></summary>
      <div><p>previous <code>{shortHash(event.previous_hash)}</code></p><JsonBlock value={event.payload} /></div>
    </details>
  ))}</section>;
}

function SummaryView({summary, status}: {summary: Record<string, JsonValue>; status: JobStatus}) {
  if (status !== "complete" || !summary || Object.keys(summary).length === 0) {
    return <Empty text="只有全部硬门禁和做种核验通过后，任务才会生成不可变完成总结。当前状态不会被包装成成功。" />;
  }
  const source = asRecord(summary.source);
  const target = asRecord(summary.target);
  const seeding = asRecord(summary.seeding);
  const audit = asRecord(summary.audit);
  return <section className="summary-view">
    <div className="summary-grid">
      <article><p className="eyebrow">SOURCE</p><strong>{String(source.site_code ?? "—")} #{String(source.torrent_id ?? "—")}</strong><span>{String(source.name ?? "")}</span></article>
      <article><p className="eyebrow">TARGET</p><strong>{String(target.site_code ?? "—")} #{String(target.torrent_id ?? "—")}</strong><span>{String(target.details_url ?? "")}</span></article>
      <article><p className="eyebrow">SEEDING</p><strong>{String(seeding.downloader_name ?? "—")}</strong><span>{shortHash(String(seeding.torrent_hash ?? ""))}</span></article>
      <article><p className="eyebrow">EVIDENCE</p><strong>{String(audit.artifact_count ?? "0")} artifacts</strong><span>规则、查重、上传、注入与做种均已绑定</span></article>
    </div>
    <JsonBlock value={summary} />
  </section>;
}

function CreateJobDialog({client, onClose, onCreated, onError}: {
  client: ApiClient;
  onClose: () => void;
  onCreated: (jobID: string) => void;
  onError: (reason: unknown) => void;
}) {
  const [form, setForm] = useState<CreateJobInput>({
    sourceURL: "", target: "MTEAM", executionMode: "step", stopAfterStep: "",
		downloaderName: "default", savePath: "/downloads", applyLabels: true, screenshotProfile: "default", imageHost: "default",
  });
  const [busy, setBusy] = useState(false);
  const update = <K extends keyof CreateJobInput>(key: K, value: CreateJobInput[K]) => setForm((current) => ({...current, [key]: value}));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await client.createJob(form);
      onCreated(created.job_id);
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
    <section className="dialog-card" role="dialog" aria-modal="true" aria-labelledby="create-title">
      <header><div><p className="eyebrow">NEW RETORRENT</p><h2 id="create-title">创建转种任务</h2></div><button className="close-button" onClick={onClose} aria-label="关闭">×</button></header>
      <form onSubmit={(event) => void submit(event)}>
        <label className="full">源站详情链接<input type="url" required placeholder="https://u2.dmhy.org/details.php?id=…" value={form.sourceURL} onChange={(event) => update("sourceURL", event.target.value)} /></label>
        <label>目标站<input required value={form.target} onChange={(event) => update("target", event.target.value.toUpperCase())} /></label>
        <label>执行模式<select value={form.executionMode} onChange={(event) => update("executionMode", event.target.value as "auto" | "step")}><option value="step">逐步暂停（推荐）</option><option value="auto">自动执行到硬门禁</option></select></label>
        <label>下载器名称<input required value={form.downloaderName} onChange={(event) => update("downloaderName", event.target.value)} /></label>
        <label>远程保存路径<input required value={form.savePath} onChange={(event) => update("savePath", event.target.value)} /></label>
				<label className="full"><input type="checkbox" checked={form.applyLabels} onChange={(event) => update("applyLabels", event.target.checked)} /> 应用下载器分类和标签（Deluge 核心 API 等不支持标签的适配器必须取消）</label>
        <label>截图配置<input required value={form.screenshotProfile} onChange={(event) => update("screenshotProfile", event.target.value)} /></label>
        <label>图床配置<input required value={form.imageHost} onChange={(event) => update("imageHost", event.target.value)} /></label>
        <label className="full">指定暂停步骤（可选）<input value={form.stopAfterStep} placeholder="例如 target_duplicate_check" onChange={(event) => update("stopAfterStep", event.target.value)} /></label>
        <div className="safety-callout full"><strong>安全默认值</strong><span>新任务不会附带规则接受，也不会确认 live 上传。流程会在对应硬门禁停下，等待人工提交指纹或显式确认。</span></div>
        <footer className="full"><button type="button" className="secondary" onClick={onClose}>取消</button><button type="submit" className="primary" disabled={busy}>{busy ? "创建中…" : "创建任务"}</button></footer>
      </form>
    </section>
  </div>;
}

function WelcomePanel({onCreate}: {onCreate: () => void}) {
  return <section className="welcome-panel"><p className="eyebrow">AUDITABLE BY DESIGN</p><h1>每一步都可停、可查、可恢复。</h1><p>创建任务后，服务会依次验证源站、规则、内容、素材、目标查重、上传包、live 确认和做种义务。任何缺口都会成为明确 blocker。</p><button className="primary" onClick={onCreate}>创建第一个任务</button></section>;
}

function DetailSkeleton() {
  return <div className="detail-skeleton"><i /><i /><i /><i /></div>;
}

function StatusPill({status}: {status: JobStatus | string}) {
  return <span className={`status-pill status-${status}`}><i />{statusLabels[status as JobStatus] ?? status}</span>;
}

function JsonBlock({value, emptyLabel}: {value: JsonValue; emptyLabel?: string}) {
  const empty = value == null || (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0);
  if (empty && emptyLabel) return <p className="empty-json">{emptyLabel}</p>;
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>;
}

function Empty({text}: {text: string}) {
  return <div className="empty"><div className="empty-ring" /><p>{text}</p></div>;
}

function humanizeStep(key: string): string {
  const labels: Record<string, string> = {
    source_parse: "识别源站链接", source_inspect: "读取源站信息", source_rules: "验证源站规则",
    source_torrent: "获取源站种子", downloader_add: "加入下载器", downloader_wait: "等待下载完成",
    content_resolve: "解析内容与路径", metadata: "收集元数据", media_info: "生成媒体信息",
    screenshots: "生成截图", image_upload: "上传图片", target_package: "生成目标描述包",
    target_duplicate_check: "目标站查重", target_rules: "验证目标站规则", target_torrent: "生成目标种子",
    target_upload: "上传目标站", target_torrent_download: "下载目标站新种", target_inject: "注入目标站做种",
    target_seed_verify: "核验做种义务", summary: "生成闭环总结",
    candidate_rules: "冻结候选规则", candidate_scan: "扫描源站候选",
    candidate_evaluate: "评估元数据与查重", candidate_rank: "排名并持久化", candidate_summary: "生成候选总结",
  };
  return labels[key] ?? key.replaceAll("_", " ");
}

function formatDate(value?: string, compact = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", compact ? {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"} : {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(date);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function shortID(value: string): string { return value ? value.slice(0, 8) : "—"; }
function shortHash(value?: string): string { return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : "—"; }
function asRecord(value: JsonValue | undefined): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}
