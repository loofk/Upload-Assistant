import {FormEvent, useCallback, useEffect, useState} from "react";
import type {ApiClient} from "./api";
import type {Blocker, DailyCandidate, DailyCandidateSchedule, DailyCandidateScheduleRun, Notification} from "./types";

export default function Candidates({client, onJobCreated, onError}: {
  client: ApiClient;
  onJobCreated: (jobID: string) => void;
  onError: (reason: unknown) => void;
}) {
  const [source, setSource] = useState("U2");
  const [target, setTarget] = useState("MTEAM");
  const [date, setDate] = useState("");
  const [items, setItems] = useState<DailyCandidate[]>([]);
  const [schedules, setSchedules] = useState<DailyCandidateSchedule[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [runs, setRuns] = useState<DailyCandidateScheduleRun[]>([]);
  const [runScheduleName, setRunScheduleName] = useState("");
  const [scheduleName, setScheduleName] = useState("u2-mteam-daily");
  const [scheduleTime, setScheduleTime] = useState("09:00");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [scheduleChannels, setScheduleChannels] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitting, setSubmitting] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [candidateResponse, scheduleResponse, notificationResponse] = await Promise.all([
        client.listDailyCandidates({source, target, date: date || undefined, limit: 50}),
        client.listDailyCandidateSchedules(), client.listNotifications(),
      ]);
      setItems(candidateResponse.candidates);
      setSchedules(scheduleResponse.schedules);
      setNotifications(notificationResponse.notifications);
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  }, [client, date, onError, source, target]);

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const createScan = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await client.createDailyCandidateJob({source, target, targetCount: 10, scanLimit: 30, date: date || undefined});
      onJobCreated(created.job_id);
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const createSchedule = async (event: FormEvent) => {
    event.preventDefault();
    const [hour, minute] = scheduleTime.split(":").map(Number);
    setBusy(true);
    try {
      await client.createDailyCandidateSchedule({
        name: scheduleName, source, target, timezone,
        cronExpression: `${minute} ${hour} * * *`, targetCount: 10, scanLimit: 30,
        notificationChannels: scheduleChannels.split(",").map((value) => value.trim()).filter(Boolean),
      });
      await load();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const toggleSchedule = async (schedule: DailyCandidateSchedule) => {
    setBusy(true);
    try {
      await client.setDailyCandidateScheduleEnabled(schedule.id, !schedule.enabled);
      await load();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const loadRuns = async (schedule: DailyCandidateSchedule) => {
    setBusy(true);
    try {
      const response = await client.listDailyCandidateScheduleRuns(schedule.id);
      setRuns(response.runs);
      setRunScheduleName(schedule.name);
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const submit = async (candidateID: string) => {
    if (!window.confirm("创建逐步暂停的转种任务？规则接受和 live 上传确认仍需后续人工提供。")) return;
    setSubmitting(candidateID);
    try {
      const created = await client.submitDailyCandidate(candidateID);
      onJobCreated(created.job_id);
    } catch (reason) {
      onError(reason);
    } finally {
      setSubmitting("");
    }
  };

  const reconcileNotification = async (notification: Notification, decision: "verified_not_delivered" | "verified_delivered") => {
    const evidenceSHA256 = window.prompt("输入人工核对证据的 lowercase SHA-256；不会上传证据原文。")?.trim() ?? "";
    if (!/^[a-f0-9]{64}$/.test(evidenceSHA256)) {
      onError(new Error("通知对账需要合法的 lowercase SHA-256。"));
      return;
    }
    const messageID = decision === "verified_delivered" ? window.prompt("输入已在 Discord 观察到的精确数字 message_id。")?.trim() ?? "" : undefined;
    if (decision === "verified_delivered" && !/^[0-9]{1,30}$/.test(messageID ?? "")) {
      onError(new Error("verified_delivered 需要精确的数字 message_id。"));
      return;
    }
    const confirmation = decision === "verified_delivered"
      ? "确认该消息已送达？系统只记录远端回执，绝不会再次调用 webhook。"
      : "确认该消息未送达并允许一次显式重试？这可能产生一条新的 Discord 消息。";
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    try {
      await client.reconcileNotification(notification.id, {
        decision, evidenceSHA256, observedAt: new Date().toISOString(), messageID,
      });
      await load();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  };

  const selected = items.filter((item) => item.rank != null).length;
  const submittable = items.filter((item) => item.status === "candidate" && item.rank != null).length;
  const blocked = items.filter((item) => item.status === "blocked" || item.status === "expired").length;
  return <main className="candidate-pane">
    <header className="candidate-header">
      <div><p className="eyebrow">DAILY DISCOVERY</p><h1>每日候选</h1><p>每批候选都经过规则快照、可下载性、元数据和目标站查重；人工 obligation 作为风险保留到正式任务。</p></div>
      <form onSubmit={(event) => void createScan(event)}>
        <label>源站<input value={source} onChange={(event) => setSource(event.target.value.toUpperCase())} required /></label>
        <label>目标站<input value={target} onChange={(event) => setTarget(event.target.value.toUpperCase())} required /></label>
        <label>日期（可选）<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>
        <button className="secondary" type="button" disabled={busy} onClick={() => void load()}>读取</button>
        <button className="primary" type="submit" disabled={busy}>创建 10 条候选任务</button>
      </form>
    </header>

    <section className="candidate-metrics">
      <div><strong>{selected}</strong><span>Top-N 已选</span></div>
      <div><strong>{submittable}</strong><span>可创建任务</span></div>
      <div><strong>{blocked}</strong><span>有阻塞或过期</span></div>
      <p>候选提交永远不会继承站规接受，也会强制 <code>confirm_upload=false</code>。</p>
    </section>

    <section className="candidate-automation">
      <form onSubmit={(event) => void createSchedule(event)}>
        <div><p className="eyebrow">DURABLE SCHEDULE</p><h2>每日自动扫描</h2><span>仅创建候选任务和已选通知，不会提交候选或上传种子。</span></div>
        <label>名称<input value={scheduleName} maxLength={100} onChange={(event) => setScheduleName(event.target.value)} required /></label>
        <label>每天时间<input type="time" value={scheduleTime} onChange={(event) => setScheduleTime(event.target.value)} required /></label>
        <label>时区<input value={timezone} onChange={(event) => setTimezone(event.target.value)} required /></label>
        <label>外部通知渠道（逗号分隔，可空）<input value={scheduleChannels} onChange={(event) => setScheduleChannels(event.target.value)} placeholder="discord-main" /></label>
        <button className="secondary" disabled={busy}>保存调度</button>
      </form>
      <div className="schedule-list">
        {schedules.map((schedule) => <article key={schedule.id}>
          <div><strong>{schedule.name}</strong><span>{schedule.config.source} → {schedule.config.target} · {schedule.cron_expression} · {schedule.timezone} · 通知 {(schedule.config.notification_channels ?? []).join(", ") || "仅本地"}</span></div>
          <span>下次 {schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString("zh-CN") : "未计划"}</span>
          <div className="schedule-actions"><button className="ghost compact" disabled={busy} onClick={() => void loadRuns(schedule)}>运行记录</button><button className="ghost compact" disabled={busy} onClick={() => void toggleSchedule(schedule)}>{schedule.enabled ? "停用" : "启用"}</button></div>
        </article>)}
        {schedules.length === 0 && <p className="empty compact-empty">尚未配置每日调度。</p>}
        {runScheduleName && <div className="schedule-runs"><h3>{runScheduleName} · 最近运行</h3>
          {runs.map((run) => <p key={run.id}><span>{new Date(run.scheduled_for).toLocaleString("zh-CN")} · {run.status} · 尝试 {run.attempts}</span>{run.job_id ? <button className="ghost compact" onClick={() => onJobCreated(run.job_id!)}>查看任务</button> : <code>{run.last_error || "尚未创建任务"}</code>}</p>)}
          {runs.length === 0 && <p><span>尚无运行记录。</span></p>}
        </div>}
      </div>
      <div className="candidate-notifications">
        <h3>通知与投递审计</h3>
        {notifications.slice(0, 5).map((notification) => <article key={notification.id}>
          <span>{notificationLabel(notification)} · {notification.channel} · {notification.status}</span><code>{notification.job_id?.slice(0, 8) || "无任务"}</code>
          <time>{new Date(notification.created_at).toLocaleString("zh-CN")}</time>
		  {notification.status === "outcome_unknown" && <div className="schedule-actions">
			<button className="ghost compact" disabled={busy} onClick={() => void reconcileNotification(notification, "verified_not_delivered")}>确认未送达并重试</button>
			<button className="ghost compact" disabled={busy} onClick={() => void reconcileNotification(notification, "verified_delivered")}>确认已送达</button>
		  </div>}
        </article>)}
        {notifications.length === 0 && <p>暂无已完成的调度通知。</p>}
      </div>
    </section>

    <section className="candidate-grid" aria-busy={busy}>
      {items.map((item) => <CandidateCard key={item.id} item={item} busy={submitting === item.id} onSubmit={submit} />)}
      {!busy && items.length === 0 && <div className="empty candidate-empty">当天还没有候选。先创建扫描任务，再到任务页查看五段审计流程。</div>}
    </section>
  </main>;
}

function CandidateCard({item, busy, onSubmit}: {item: DailyCandidate; busy: boolean; onSubmit: (id: string) => Promise<void>}) {
  const source = item.payload.source ?? {};
  const metadata = item.payload.metadata ?? {};
  const blockers = item.payload.blockers ?? [];
  const risks = item.payload.risks ?? [];
  const reasons = item.payload.recommendation_reasons ?? [];
  const title = source.title || metadata.name || `${item.source_site} #${item.source_torrent_id}`;
  return <article className={`candidate-card ${item.rank ? "ranked" : ""}`}>
    <header>
      <div className="candidate-rank">{item.rank ? `#${item.rank}` : "—"}</div>
      <div><span>{item.source_site} → {item.target_site}</span><h2>{title}</h2></div>
      <strong>{Math.round(item.score)}</strong>
    </header>
    <div className="candidate-facts">
      <span>{formatBytes(source.size_bytes)}</span>
      <span>{source.published_at ? new Date(source.published_at).toLocaleString("zh-CN") : "发布时间未知"}</span>
      <span className={source.free ? "free" : ""}>{source.free ? "FREE" : (source.promotion_labels ?? []).join(" / ") || "无促销"}</span>
    </div>
    <div className="metadata-row">
      <code>{metadata.imdb_id || "IMDb —"}</code><code>{metadata.tmdb_id ? `TMDb ${metadata.tmdb_id}` : "TMDb —"}</code><code>{metadata.douban_id ? `豆瓣 ${metadata.douban_id}` : "豆瓣 —"}</code>
      <span>{item.payload.duplicate_check?.duplicate === false ? "目标查重通过" : item.payload.duplicate_check?.duplicate ? "目标已有重复" : "查重未完成"}</span>
    </div>
    {reasons.length > 0 && <div className="reason-tags">{reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
    {(risks.length > 0 || blockers.length > 0) && <div className="candidate-gates">
      {risks.map((risk) => <GateLine key={`risk-${gateKey(risk)}`} blocker={risk} risk />)}
      {blockers.map((blocker) => <GateLine key={`block-${gateKey(blocker)}`} blocker={blocker} />)}
    </div>}
    <footer>
      <span>来源 ID {item.source_torrent_id} · 到期 {new Date(item.expires_at).toLocaleString("zh-CN")}</span>
      {item.status === "candidate" && item.rank != null ? <button className="primary compact" disabled={busy} onClick={() => void onSubmit(item.id)}>{busy ? "创建中…" : "创建逐步转种任务"}</button> : <i>{item.status === "submitted" ? "已提交" : "暂不可提交"}</i>}
    </footer>
  </article>;
}

function GateLine({blocker, risk = false}: {blocker: Blocker; risk?: boolean}) {
  return <p className={risk ? "risk" : "block"}><code>{risk ? "RISK" : "BLOCK"} · {blocker.code}</code><span>{blocker.message || "需人工检查"}</span></p>;
}

function gateKey(blocker: Blocker) { return `${blocker.site_code ?? ""}-${blocker.code}-${blocker.message ?? ""}`; }

function notificationLabel(notification: Notification) {
  const name = typeof notification.payload.schedule_name === "string" ? notification.payload.schedule_name : "每日候选";
  const status = typeof notification.payload.job_status === "string" ? notification.payload.job_status : notification.status;
  return `${name} · ${status}`;
}

function formatBytes(value?: number) {
  if (!value || value < 0) return "大小未知";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}
