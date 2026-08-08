import {FormEvent, useState} from "react";
import type {ApiClient} from "./api";
import type {LiveReadinessReport} from "./types";

export default function Readiness({client, onError}: {client: ApiClient; onError: (reason: unknown) => void}) {
  const [source, setSource] = useState<"U2" | "CHD">("U2");
  const [downloader, setDownloader] = useState("box");
  const [targetDownloader, setTargetDownloader] = useState("");
  const [imageHost, setImageHost] = useState("imgbb");
  const [screenshotProfile, setScreenshotProfile] = useState("default");
  const [tmdbProvider, setTMDbProvider] = useState("tmdb-main");
  const [ptgenProvider, setPTGenProvider] = useState("ptgen-main");
  const [report, setReport] = useState<LiveReadinessReport | null>(null);
  const [loading, setLoading] = useState(false);

  const check = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    try {
      setReport(await client.getLiveReadiness({source, target: "MTEAM", downloader, targetDownloader, imageHost, screenshotProfile, tmdbProvider, ptgenProvider}));
    } catch (reason) {
      onError(reason);
    } finally {
      setLoading(false);
    }
  };

  return <main className="readiness-pane">
    <header className="readiness-header">
      <div><p className="eyebrow">LOCAL-ONLY PREFLIGHT</p><h1>真实环境就绪检查</h1><p>只读取本地规则、凭据字段、集成配置、工具与挂载状态。不会连接站点、下载器或图床，也不会授权上传。</p></div>
    </header>
    <form className="readiness-form" onSubmit={(event) => void check(event)}>
      <label>源站<select value={source} onChange={(event) => setSource(event.target.value as "U2" | "CHD")}><option>U2</option><option>CHD</option></select></label>
      <label>目标站<select value="MTEAM" disabled><option>MTEAM</option></select></label>
      <label>源下载器<input required value={downloader} onChange={(event) => setDownloader(event.target.value)} /></label>
      <label>目标下载器<input value={targetDownloader} onChange={(event) => setTargetDownloader(event.target.value)} placeholder="留空则同源下载器" /></label>
      <label>图床<input required value={imageHost} onChange={(event) => setImageHost(event.target.value)} /></label>
      <label>截图策略<input required value={screenshotProfile} onChange={(event) => setScreenshotProfile(event.target.value)} /></label>
      <label>TMDb 提供方<input required value={tmdbProvider} onChange={(event) => setTMDbProvider(event.target.value)} /></label>
      <label>PTGen 提供方<input required value={ptgenProvider} onChange={(event) => setPTGenProvider(event.target.value)} /></label>
      <button className="primary" type="submit" disabled={loading}>{loading ? "检查中…" : "执行本地检查"}</button>
    </form>
    <section className="readiness-safety" aria-label="安全边界">
      <article><span>外部调用</span><strong>始终为否</strong></article>
      <article><span>上传授权</span><strong>始终为否</strong></article>
      <article><span>确认上传</span><strong>必须后续显式提供</strong></article>
    </section>
    {!report && <div className="empty readiness-empty">填写本机资源名称后执行检查；此操作不会联网。</div>}
    {report && <>
      <section className={`readiness-result ${report.configuration_ready ? "ready" : "blocked"}`}>
        <div><p className="eyebrow">{report.status}</p><h2>{report.configuration_ready ? "本地配置已就绪" : `发现 ${report.blockers.length} 个阻塞项`}</h2><p>{report.summary}</p></div>
        <dl><div><dt>外部调用</dt><dd>{report.external_calls_performed ? "是" : "否"}</dd></div><div><dt>上传授权</dt><dd>{report.live_upload_authorized ? "是" : "否"}</dd></div><div><dt>confirm_upload</dt><dd>{report.resume_state.confirm_upload ? "true" : "false"}</dd></div></dl>
      </section>
      {report.blockers.length > 0 && <section className="readiness-blockers"><h2>阻塞项</h2>{report.blockers.map((blocker, index) => <article key={`${blocker.code}-${index}`}><code>{blocker.code}</code><strong>{blocker.component ?? "configuration"}</strong><p>{blocker.message}</p></article>)}</section>}
      <section className="readiness-grid" aria-label="检查明细">{report.checks.map((check) => <details key={check.key} className="readiness-check"><summary><i className={check.status} /> <code>{check.key}</code><span>{check.summary}</span></summary>{check.evidence && <pre className="json-block">{JSON.stringify(check.evidence, null, 2)}</pre>}</details>)}</section>
      <section className="readiness-confirmations"><h2>后续必须精确确认的规则</h2>{report.required_confirmations.map((confirmation) => <article key={confirmation.site_code}><strong>{confirmation.site_code}</strong><code>{confirmation.fingerprint}</code><span>{confirmation.obligation_ids.length ? confirmation.obligation_ids.join(" · ") : "无阻塞 obligation"}</span></article>)}</section>
      <details className="readiness-resume"><summary>安全续跑模板（尚未接受规则、尚未确认上传）</summary><pre className="json-block">{JSON.stringify(report.resume_state, null, 2)}</pre></details>
    </>}
  </main>;
}
