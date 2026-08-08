import {FormEvent, ReactNode, useCallback, useEffect, useState} from "react";
import {ApiClient} from "./api";
import type {AdapterCatalogEnvelope, Downloader, DownloaderAdapterCapability, ImageHost, LegacyMigrationPreview, LegacyMigrationRecord, MediaManager, MetadataProvider, NotificationChannel, RuleRevision, ScreenshotProfile, SiteCredential, SiteSummary} from "./types";

type ConfigTab = "capabilities" | "downloaders" | "image-hosts" | "notifications" | "media-managers" | "metadata-providers" | "screenshots" | "rules" | "migration";

export default function Configuration({client, onError}: {client: ApiClient; onError: (reason: unknown) => void}) {
  const [tab, setTab] = useState<ConfigTab>("downloaders");
	const [catalog, setCatalog] = useState<AdapterCatalogEnvelope | null>(null);
  const [downloaders, setDownloaders] = useState<Downloader[]>([]);
	const [downloaderAdapters, setDownloaderAdapters] = useState<DownloaderAdapterCapability[]>([]);
  const [imageHosts, setImageHosts] = useState<ImageHost[]>([]);
  const [notificationChannels, setNotificationChannels] = useState<NotificationChannel[]>([]);
  const [mediaManagers, setMediaManagers] = useState<MediaManager[]>([]);
  const [metadataProviders, setMetadataProviders] = useState<MetadataProvider[]>([]);
  const [screenshots, setScreenshots] = useState<ScreenshotProfile[]>([]);
  const [sites, setSites] = useState<SiteSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
		const [nextCatalog, nextDownloaders, nextDownloaderAdapters, nextImageHosts, nextNotificationChannels, nextMediaManagers, nextMetadataProviders, nextScreenshots, nextSites] = await Promise.all([
			client.listAdapterCapabilities(), client.listDownloaders(), client.listDownloaderAdapters(), client.listImageHosts(), client.listNotificationChannels(), client.listMediaManagers(), client.listMetadataProviders(), client.listScreenshotProfiles(), client.listSites(),
      ]);
		setCatalog(nextCatalog);
      setDownloaders(nextDownloaders);
		setDownloaderAdapters(nextDownloaderAdapters);
      setImageHosts(nextImageHosts);
      setNotificationChannels(nextNotificationChannels);
      setMediaManagers(nextMediaManagers);
      setMetadataProviders(nextMetadataProviders);
      setScreenshots(nextScreenshots);
      setSites(nextSites);
    } catch (reason) {
      onError(reason);
    } finally {
      setLoading(false);
    }
  }, [client, onError]);

  useEffect(() => { void reload(); }, [reload]);

  return <main className="configuration-pane">
    <header className="configuration-header">
      <div><p className="eyebrow">INDEPENDENT INTEGRATIONS</p><h1>配置中心</h1><p>凭据只写入加密存储，列表仅显示字段名；任何更新都会留下审计事件。</p></div>
      <button className="secondary" onClick={() => void reload()} disabled={loading}>刷新配置</button>
    </header>
    <nav className="config-tabs">
      {(["capabilities", "downloaders", "image-hosts", "notifications", "media-managers", "metadata-providers", "screenshots", "rules", "migration"] as const).map((value) => <button key={value} className={tab === value ? "active" : ""} onClick={() => setTab(value)}>
        {value === "capabilities" ? `能力契约 ${catalog?.count ?? 0}` : value === "downloaders" ? `下载器 ${downloaders.length}` : value === "image-hosts" ? `图床 ${imageHosts.length}` : value === "notifications" ? `通知 ${notificationChannels.length}` : value === "media-managers" ? `Sonarr/Radarr ${mediaManagers.length}` : value === "metadata-providers" ? `元数据 ${metadataProviders.length}` : value === "screenshots" ? `截图策略 ${screenshots.length}` : value === "rules" ? `站点规则 ${sites.length}` : "旧配置迁移"}
      </button>)}
    </nav>
		{tab === "capabilities" && <AdapterCatalogPanel catalog={catalog} />}
		{tab === "downloaders" && <DownloadersPanel items={downloaders} adapters={downloaderAdapters} client={client} reload={reload} onError={onError} />}
    {tab === "image-hosts" && <ImageHostsPanel items={imageHosts} client={client} reload={reload} onError={onError} />}
    {tab === "notifications" && <NotificationChannelsPanel items={notificationChannels} client={client} reload={reload} onError={onError} />}
    {tab === "media-managers" && <MediaManagersPanel items={mediaManagers} client={client} reload={reload} onError={onError} />}
    {tab === "metadata-providers" && <MetadataProvidersPanel items={metadataProviders} client={client} reload={reload} onError={onError} />}
    {tab === "screenshots" && <ScreenshotsPanel items={screenshots} client={client} reload={reload} onError={onError} />}
    {tab === "rules" && <RulesPanel sites={sites} client={client} reloadSites={reload} onError={onError} />}
    {tab === "migration" && <LegacyMigrationPanel client={client} />}
  </main>;
}

function AdapterCatalogPanel({catalog}: {catalog: AdapterCatalogEnvelope | null}) {
	if (!catalog) return <ConfigEmpty text="能力契约尚未加载。" />;
	return <section><ConfigSectionTitle title="适配器能力契约" copy="本地只读目录明确每个运行时能做什么、需要哪些凭据字段、必须经过哪些 gate；不能从站点或适配器名称推断支持。" />
		<div className="safety-callout"><strong>{catalog.catalog_version}</strong><span>contract sha256: {catalog.catalog_sha256}</span><span>修改任一 operation、gate 或 constraint 都会改变指纹并触发 golden 测试。</span></div>
		<div className="integration-grid">{catalog.adapters.map((item) => <IntegrationCard key={item.id} title={item.site_code ? `${item.site_code} · ${item.display_name}` : item.display_name} type={`${item.kind} / ${item.adapter}`} enabled={item.runtime_supported} health={item.runtime_supported ? "callable" : "config-only"} endpoint={item.operations.length ? `operations: ${item.operations.join(", ")}` : "没有可调用 operation"} credentials={item.credential_fields} details={[...item.safety_gates.map((gate) => `gate: ${gate}`), ...item.constraints, ...(item.unavailable_reason ? [item.unavailable_reason] : [])]} />)}</div>
	</section>;
}

function LegacyMigrationPanel({client}: {client: ApiClient}) {
  const [preview, setPreview] = useState<LegacyMigrationPreview | null>(null);
  const [imports, setImports] = useState<LegacyMigrationRecord[]>([]);
  const [error, setError] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    const [previewResult, importsResult] = await Promise.allSettled([
      client.previewLegacyMigration(), client.listLegacyMigrations(),
    ]);
    if (previewResult.status === "fulfilled") {
      setPreview(previewResult.value);
      setError("");
    } else {
      setPreview(null);
      setError(previewResult.reason instanceof Error ? previewResult.reason.message : "旧配置预览失败。");
    }
    if (importsResult.status === "fulfilled") setImports(importsResult.value);
    setBusy(false);
  }, [client]);
  useEffect(() => { void load(); }, [load]);
  const execute = async () => {
    if (!preview || !reviewed) return;
    if (!window.confirm("确认以当前 source_fingerprint 执行迁移？这会写入加密配置，但不会联网或删除旧文件。")) return;
    setBusy(true);
    try {
      await client.executeLegacyMigration(preview.source_fingerprint);
      setReviewed(false);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "迁移执行失败。");
    } finally {
      setBusy(false);
    }
  };
  return <section className="migration-panel">
    <header className="migration-hero"><div><p className="eyebrow">SAFE LEGACY IMPORT</p><h2>Python 配置迁移</h2><p>只读取固定只读挂载中的 <code>config.py</code> 与中文站点 cookie；不启动 Python、不自动联网、不删除原文件。</p></div><button className="secondary" disabled={busy} onClick={() => void load()}>重新预览</button></header>
    {error && <div className="migration-blocked"><strong>当前无法预览</strong><p>{error}</p><small>请通过 UA_LEGACY_DATA_HOST_PATH 把旧 data 目录只读挂载到 /legacy。</small></div>}
    {preview && <div className="migration-grid"><article className="migration-preview"><header><div><strong>{preview.status === "ready" ? "可执行预览" : "预览被阻塞"}</strong><span>{preview.resources.length} 个资源 · {preview.source_files.length} 个源文件</span></div><i className={preview.ok ? "ready" : "blocked"}>{preview.status}</i></header><label>源指纹<code>{preview.source_fingerprint}</code></label><div className="migration-resources">{preview.resources.map((resource) => <div key={`${resource.kind}:${resource.name}`}><strong>{resource.name}</strong><span>{resource.kind}{resource.adapter ? ` · ${resource.adapter}` : ""} · {resource.enabled ? "enabled" : "disabled"}</span><small>secret fields: {resource.credential_fields?.join(", ") || "none"}</small></div>)}</div>{preview.warnings.length > 0 && <div className="migration-warnings"><strong>需要后续人工处理</strong>{preview.warnings.map((issue, index) => <p key={`${issue.code}:${issue.resource}:${index}`}><code>{issue.code}</code>{issue.resource ? ` · ${issue.resource}` : ""} — {issue.message}</p>)}</div>}<footer><span>加密归档 {preview.archive.retention_days} 天；API 永不提供归档明文。</span><label><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} /> 我已核对源指纹、资源清单和所有 warnings</label><button className="primary" disabled={busy || !reviewed || !preview.ok} onClick={() => void execute()}>{busy ? "执行中…" : "确认执行迁移"}</button></footer></article>
      <aside className="migration-files"><h3>源证据</h3>{preview.source_files.map((file) => <div key={file.path}><strong>{file.path}</strong><code title={file.fingerprint}>{file.fingerprint}</code><span>{formatBytes(file.size_bytes)} · keyed fingerprint</span></div>)}</aside></div>}
    <section className="migration-history"><ConfigSectionTitle title="迁移历史" copy="仅显示脱敏报告、资源 ID 和归档保留状态；不提供凭据或归档明文。" />{imports.map((item) => <article key={item.id}><header><div><strong>{item.status}</strong><span>{new Date(item.created_at).toLocaleString("zh-CN")}</span></div><i className={item.archive_available ? "ready" : "expired"}>{item.archive_available ? "archive retained" : "archive expired"}</i></header><code title={item.source_fingerprint}>{item.source_fingerprint}</code><p>{item.report.summary}</p><small>已配置 {item.report.applied.length} 个资源 · 归档到期 {new Date(item.archive_expires_at).toLocaleString("zh-CN")}</small></article>)}{!imports.length && <ConfigEmpty text="暂无迁移历史。" />}</section>
  </section>;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function DownloadersPanel({items, adapters, client, reload, onError}: {items: Downloader[]; adapters: DownloaderAdapterCapability[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
	const [form, setForm] = useState({name: "default", adapter: "qbittorrent", enabled: true, endpoint: "http://host.docker.internal:8080", username: "", password: "", apiKey: "", remote: "/downloads", local: "/downloads"});
  const [busy, setBusy] = useState(false);
	const selectedCapability = adapters.find((item) => item.adapter === form.adapter);
	const supportsCredential = (field: string) => selectedCapability?.credential_fields.includes(field) ?? false;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const credentials: Record<string, string> = {};
			if (supportsCredential("api_key") && form.apiKey) credentials.api_key = form.apiKey;
			if (supportsCredential("username")) {
				if ((form.username && !form.password) || (!form.username && form.password)) throw new Error(`${selectedCapability?.display_name ?? form.adapter} 的用户名与密码必须同时填写；更新时可同时留空以保留现有凭据。`);
				if (form.username && form.password) {
					credentials.username = form.username;
					credentials.password = form.password;
				}
			} else if (supportsCredential("password") && form.password) {
				credentials.password = form.password;
			}
      await client.putDownloader(form.name, {
			adapter: form.adapter, enabled: form.enabled, endpoint: form.endpoint, credentials,
        pathMappings: form.remote && form.local ? [{remote_path: form.remote, local_path: form.local, priority: 100}] : [],
      });
      setForm((current) => ({...current, username: "", password: "", apiKey: ""}));
      await reload();
    } catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <div className="config-layout"><section><ConfigSectionTitle title="远程下载器" copy="每个实例有独立 endpoint、加密凭据和远程→容器路径映射。" />
		<div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter} enabled={item.enabled} health={item.health_status} endpoint={item.config.endpoint} credentials={item.credential_fields} details={[...item.path_mappings.map((mapping) => `${mapping.remote_path} → ${mapping.local_path}`), ...(item.adapter_capability?.constraints ?? []), ...(item.adapter_capability?.unavailable_reason ? [item.adapter_capability.unavailable_reason] : [])]} action={<button className="card-action" disabled={!item.enabled || !item.adapter_capability?.runtime_supported} onClick={async () => { try { await client.probeDownloader(item.name); await reload(); } catch (reason) { onError(reason); } }}>显式探测</button>} />)}{!items.length && <ConfigEmpty text="尚未配置下载器。" />}</div>
  </section><ConfigForm title="添加或更新下载器" onSubmit={submit} busy={busy}>
    <label>配置名称<input value={form.name} required onChange={(event) => setForm({...form, name: event.target.value})} /></label>
		<label>适配器<select value={form.adapter} onChange={(event) => { const adapter = event.target.value; const capability = adapters.find((item) => item.adapter === adapter); const endpoints: Record<string, string> = {qbittorrent: "http://host.docker.internal:8080", transmission: "http://host.docker.internal:9091/transmission/rpc", rtorrent: "http://host.docker.internal/RPC2", deluge: "http://host.docker.internal:8112/json"}; setForm({...form, adapter, endpoint: endpoints[adapter] ?? form.endpoint, enabled: capability?.runtime_supported ?? false, username: "", password: "", apiKey: ""}); }}>{adapters.map((item) => <option key={item.adapter} value={item.adapter}>{item.display_name}{item.runtime_supported ? "" : "（仅可禁用保存）"}</option>)}</select></label>
		<label><input type="checkbox" checked={form.enabled} disabled={!adapters.find((item) => item.adapter === form.adapter)?.runtime_supported} onChange={(event) => setForm({...form, enabled: event.target.checked})} /> 启用运行时</label>
    <label className="full">服务地址<input type="url" value={form.endpoint} required onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>
		{supportsCredential("username") && <label>用户名（与密码同时填写）<input autoComplete="off" value={form.username} onChange={(event) => setForm({...form, username: event.target.value})} /></label>}
		{supportsCredential("password") && <label>{supportsCredential("username") ? "密码（与用户名同时填写）" : "Web 密码（新建必填）"}<input type="password" autoComplete="new-password" value={form.password} onChange={(event) => setForm({...form, password: event.target.value})} /></label>}
		{supportsCredential("api_key") && <label className="full">API Key（可选、留空则保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>}
		{selectedCapability?.constraints?.length ? <div className="safety-callout full"><strong>适配器约束</strong>{selectedCapability.constraints.map((constraint) => <span key={constraint}>{constraint}</span>)}</div> : null}
    <label>远程路径<input value={form.remote} onChange={(event) => setForm({...form, remote: event.target.value})} /></label><label>容器路径<input value={form.local} onChange={(event) => setForm({...form, local: event.target.value})} /></label>
  </ConfigForm></div>;
}

function ImageHostsPanel({items, client, reload, onError}: {items: ImageHost[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const [form, setForm] = useState({name: "default", adapter: "imgbb", endpoint: "https://api.imgbb.com/1/upload", apiKey: "", priority: 100});
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putImageHost(form.name, form); setForm({...form, apiKey: ""}); await reload(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <div className="config-layout"><section><ConfigSectionTitle title="独立图床" copy="按 priority 选择启用实例；API key 从不回显。" />
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter} enabled={item.enabled} health={item.health_status} endpoint={item.config.endpoint} credentials={item.credential_fields} details={[`优先级 ${item.priority}`]} />)}{!items.length && <ConfigEmpty text="尚未配置图床。" />}</div>
  </section><ConfigForm title="添加或更新图床" onSubmit={submit} busy={busy}>
    <label>配置名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>适配器<select value={form.adapter} onChange={(event) => { const adapter = event.target.value; setForm({...form, adapter, endpoint: adapter === "ptpimg" ? "https://ptpimg.me" : "https://api.imgbb.com/1/upload"}); }}><option value="imgbb">imgbb</option><option value="ptpimg">PTPimg</option></select></label>
    <label className="full">API 地址<input type="url" required value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>
    <label>优先级<input type="number" value={form.priority} onChange={(event) => setForm({...form, priority: Number(event.target.value)})} /></label>
    <label>API Key（留空则保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>
  </ConfigForm></div>;
}

function NotificationChannelsPanel({items, client, reload, onError}: {items: NotificationChannel[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const [form, setForm] = useState({name: "discord-main", enabled: true, webhookURL: ""});
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putNotificationChannel(form.name, form); setForm({...form, webhookURL: ""}); await reload(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <div className="config-layout"><section><ConfigSectionTitle title="Discord 通知渠道" copy="Webhook URL 加密保存；只有调度显式选择此渠道时才会投递，失败按持久队列重试。" />
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter} enabled={item.enabled} health={item.health_status} endpoint="encrypted webhook URL" credentials={item.credential_fields} details={[`超时 ${item.config.timeout_seconds ?? 15}s`, "禁止 mentions · 保存送达回执 hash"]} />)}{!items.length && <ConfigEmpty text="尚未配置通知渠道；每日候选仍会保留本地通知。" />}</div>
  </section><ConfigForm title="添加或更新 Discord webhook" onSubmit={submit} busy={busy}>
    <label>渠道名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({...form, enabled: event.target.checked})} /> 启用投递</label>
    <label className="full">Webhook URL（新建必填，更新留空保留）<input type="password" autoComplete="new-password" value={form.webhookURL} onChange={(event) => setForm({...form, webhookURL: event.target.value})} /></label>
    <div className="safety-callout full"><strong>安全边界</strong><span>只发送候选摘要；不会提交候选、确认规则或上传种子。</span></div>
  </ConfigForm></div>;
}

function MediaManagersPanel({items, client, reload, onError}: {items: MediaManager[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const [form, setForm] = useState<{name: string; adapter: "sonarr" | "radarr"; enabled: boolean; endpoint: string; apiKey: string}>({name: "sonarr-main", adapter: "sonarr", enabled: true, endpoint: "http://host.docker.internal:8989", apiKey: ""});
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putMediaManager(form.name, form); setForm({...form, apiKey: ""}); await reload(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <div className="config-layout"><section><ConfigSectionTitle title="Sonarr / Radarr" copy="每个实例独立 endpoint 与加密 API key；探测和路径匹配都是显式、只读、可审计调用。" />
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter} enabled={item.enabled} health={item.health_status} endpoint={item.config.endpoint} credentials={item.credential_fields} details={["API v3 · X-Api-Key", "审计只保存查询/响应 hash"]} action={<button className="card-action" disabled={!item.enabled} onClick={async () => { try { await client.probeMediaManager(item.name); await reload(); } catch (reason) { onError(reason); } }}>显式探测</button>} />)}{!items.length && <ConfigEmpty text="尚未配置 Sonarr/Radarr。" />}</div>
  </section><ConfigForm title="添加或更新媒体管理器" onSubmit={submit} busy={busy}>
    <label>实例名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>适配器<select value={form.adapter} onChange={(event) => { const adapter = event.target.value as "sonarr" | "radarr"; setForm({...form, adapter, endpoint: adapter === "sonarr" ? "http://host.docker.internal:8989" : "http://host.docker.internal:7878"}); }}><option value="sonarr">Sonarr</option><option value="radarr">Radarr</option></select></label>
    <label><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({...form, enabled: event.target.checked})} /> 启用实例</label>
    <label className="full">服务地址<input type="url" required value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>
    <label className="full">API Key（新建必填，更新留空保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>
  </ConfigForm></div>;
}

function MetadataProvidersPanel({items, client, reload, onError}: {items: MetadataProvider[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const [form, setForm] = useState<{name: string; adapter: "tmdb" | "ptgen"; enabled: boolean; endpoint: string; apiKey: string}>({name: "tmdb-main", adapter: "tmdb", enabled: true, endpoint: "https://api.themoviedb.org", apiKey: ""});
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putMetadataProvider(form.name, form); setForm({...form, apiKey: ""}); await reload(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <div className="config-layout"><section><ConfigSectionTitle title="TMDb / PTGen 元数据" copy="每个 provider 独立 endpoint 与加密 key；只有任务或显式解析调用才会访问外部服务，审计保存 hash 而非原始响应。" />
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter} enabled={item.enabled} health={item.health_status} endpoint={item.config.endpoint} credentials={item.credential_fields} details={[item.adapter === "tmdb" ? "官方 API v3" : "显式 PTGen /api endpoint", "禁重定向 · 有界响应 · 可审计"]} />)}{!items.length && <ConfigEmpty text="尚未配置元数据 provider；任务不会隐式调用公共 PTGen。" />}</div>
  </section><ConfigForm title="添加或更新元数据 provider" onSubmit={submit} busy={busy}>
    <label>配置名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>适配器<select value={form.adapter} onChange={(event) => { const adapter = event.target.value as "tmdb" | "ptgen"; setForm({...form, adapter, endpoint: adapter === "tmdb" ? "https://api.themoviedb.org" : "", name: adapter === "tmdb" ? "tmdb-main" : "ptgen-main"}); }}><option value="tmdb">TMDb</option><option value="ptgen">PTGen</option></select></label>
    <label><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({...form, enabled: event.target.checked})} /> 启用 provider</label>
    <label className="full">API 地址<input type="url" required placeholder={form.adapter === "ptgen" ? "https://your-ptgen.example/api" : "https://api.themoviedb.org"} value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>
    <label className="full">API Key（TMDb 新建必填；PTGen 可选；更新留空保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>
    <div className="safety-callout full"><strong>外部边界</strong><span>保存配置不会发起网络请求；解析需通过任务或 API 显式触发，失败不会被包装成成功。</span></div>
  </ConfigForm></div>;
}

function ScreenshotsPanel({items, client, reload, onError}: {items: ScreenshotProfile[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const [form, setForm] = useState({name: "default", count: 6, format: "png", width: 0, quality: 90, startPercent: .1, endPercent: .9});
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { await client.createScreenshotProfile(form); await reload(); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  return <div className="config-layout"><section><ConfigSectionTitle title="不可变截图策略" copy="同名保存会创建新 revision，历史任务继续绑定旧版本。" />
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={`${item.name} · r${item.revision}`} type={String(item.config.format ?? "png")} enabled={item.enabled} health="immutable" endpoint={`${String(item.config.count ?? 0)} 张 · ${String(item.config.width ?? 0)} px`} credentials={[]} details={[`${String(item.config.start_percent ?? 0)}–${String(item.config.end_percent ?? 1)} 时段`, `质量 ${String(item.config.quality ?? 90)}`]} />)}{!items.length && <ConfigEmpty text="尚未配置截图策略。" />}</div>
  </section><ConfigForm title="创建截图策略 revision" onSubmit={submit} busy={busy}>
    <label>策略名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label><label>格式<select value={form.format} onChange={(event) => setForm({...form, format: event.target.value})}><option>png</option><option>jpg</option><option>webp</option></select></label>
    <label>截图数量<input type="number" min="1" max="20" value={form.count} onChange={(event) => setForm({...form, count: Number(event.target.value)})} /></label><label>宽度（0 保持原始）<input type="number" min="0" max="3840" value={form.width} onChange={(event) => setForm({...form, width: Number(event.target.value)})} /></label>
    <label>质量<input type="number" min="1" max="100" value={form.quality} onChange={(event) => setForm({...form, quality: Number(event.target.value)})} /></label><label>起止百分比<input value={`${form.startPercent}, ${form.endPercent}`} onChange={(event) => { const [start, end] = event.target.value.split(",").map(Number); setForm({...form, startPercent: start, endPercent: end}); }} /></label>
  </ConfigForm></div>;
}

function RulesPanel({sites, client, reloadSites, onError}: {sites: SiteSummary[]; client: ApiClient; reloadSites: () => Promise<void>; onError: (reason: unknown) => void}) {
  const [siteCode, setSiteCode] = useState("");
  const [revisions, setRevisions] = useState<RuleRevision[]>([]);
  const [credentials, setCredentials] = useState<SiteCredential[]>([]);
  const [markdown, setMarkdown] = useState("");
  const [comment, setComment] = useState("已人工核对结构化策略、原始规则证据和所有 blocking obligations");
  const [credentialName, setCredentialName] = useState("cookie");
  const [credentialValue, setCredentialValue] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!siteCode && sites.length) setSiteCode(sites[0].code); }, [siteCode, sites]);
  const loadSite = useCallback(async () => {
    if (!siteCode) return;
    try { const [rules, secrets] = await Promise.all([client.listRuleRevisions(siteCode), client.listSiteCredentials(siteCode)]); setRevisions(rules); setCredentials(secrets); }
    catch (reason) { onError(reason); }
  }, [client, onError, siteCode]);
  useEffect(() => { void loadSite(); }, [loadSite]);
  useEffect(() => { setCredentialName(siteCode === "MTEAM" ? "api_key" : "cookie"); }, [siteCode]);
  const mutate = async (action: () => Promise<unknown>) => { setBusy(true); try { await action(); await Promise.all([loadSite(), reloadSites()]); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  const selectedSite = sites.find((site) => site.code === siteCode);
  return <section className="rules-panel"><div className="site-strip">{sites.map((site) => <button key={site.code} className={site.code === siteCode ? "active" : ""} onClick={() => setSiteCode(site.code)}><strong>{site.code}</strong><span>{site.active_rule_fingerprint ? "规则已激活" : "缺少活动规则"}</span></button>)}</div>
    <div className="rules-grid"><div><ConfigSectionTitle title={`${selectedSite?.name ?? siteCode} 规则 revision`} copy="import → 人工审查 fingerprint → approve → activate；四步均独立审计。" />
      <div className="rule-revisions">{revisions.map((revision) => <article className="rule-card" key={revision.id}><header><div><strong>r{revision.revision}</strong><span className={`rule-state ${revision.status}`}>{revision.status}</span></div><time>{new Date(revision.created_at).toLocaleString("zh-CN")}</time></header><code title={revision.fingerprint}>{revision.fingerprint}</code><p>{revision.source_url}</p><div className="rule-actions">{revision.status === "draft" && <button disabled={busy} onClick={() => { if (window.confirm(`确认已人工核对 ${revision.site_code} r${revision.revision}，并以该 fingerprint 批准？`)) void mutate(() => client.approveRule(revision, comment)); }}>按 fingerprint 批准</button>}{revision.status === "approved" && <button disabled={busy} onClick={() => { if (window.confirm("确认激活此规则？后续新任务将强制绑定该 fingerprint。")) void mutate(() => client.activateRule(revision.id)); }}>激活</button>}</div></article>)}{!revisions.length && <ConfigEmpty text="该站点还没有 Go v2 规则 revision。" />}</div>
      <div className="credential-box"><div><strong>站点凭据</strong><span>已配置：{credentials.length ? credentials.map((item) => `${item.name}${item.enabled ? "" : "(disabled)"}`).join("、") : "无"}</span></div><form onSubmit={(event) => { event.preventDefault(); if (!credentialValue) return; void mutate(async () => { await client.putSiteCredential(siteCode, credentialName, credentialValue); setCredentialValue(""); }); }}><input value={credentialName} onChange={(event) => setCredentialName(event.target.value)} aria-label="凭据字段名" /><input type="password" autoComplete="new-password" value={credentialValue} onChange={(event) => setCredentialValue(event.target.value)} placeholder="新值（加密保存且不回显）" required /><button className="secondary" disabled={busy}>保存</button></form></div>
    </div><aside className="rule-import"><ConfigSectionTitle title="导入结构化 Markdown" copy="粘贴完整 front matter 与原始规则正文。原文不由程序假装验证。" /><textarea spellCheck={false} value={markdown} onChange={(event) => setMarkdown(event.target.value)} placeholder="---\nkind: upload-assistant.site-rule.v1\n…\n---\n\n# 原始规则\n…" /><label>审批备注（批准时写入审计）<input value={comment} onChange={(event) => setComment(event.target.value)} /></label><button className="primary" disabled={busy || !markdown.trim()} onClick={() => void mutate(async () => { const revision = await client.importRuleMarkdown(markdown); setSiteCode(revision.site_code); setMarkdown(""); })}>导入为 draft</button><p>导入不会自动批准或激活。`source.complete=false`、未解决 blocking obligation 或缺失人工复核时，后端会继续阻止自动执行。</p></aside></div>
  </section>;
}

function IntegrationCard({title, type, enabled, health, endpoint, credentials, details, action}: {title: string; type: string; enabled: boolean; health: string; endpoint: string; credentials: string[]; details: string[]; action?: ReactNode}) {
  return <article className="integration-card"><header><div><strong>{title}</strong><span>{type}</span></div><i className={enabled ? "enabled" : ""}>{enabled ? "enabled" : "disabled"}</i></header><p>{endpoint}</p><div className="integration-meta"><span>health: {health || "unknown"}</span>{details.map((detail) => <span key={detail}>{detail}</span>)}<span>credentials: {credentials.length ? credentials.join(", ") : "none"}</span></div>{action}</article>;
}

function ConfigForm({title, onSubmit, busy, children}: {title: string; onSubmit: (event: FormEvent) => void; busy: boolean; children: ReactNode}) {
  return <aside className="config-form"><h2>{title}</h2><form onSubmit={onSubmit}>{children}<footer className="full"><span>secret 字段不会写入普通配置或日志</span><button className="primary" disabled={busy}>{busy ? "保存中…" : "保存配置"}</button></footer></form></aside>;
}

function ConfigSectionTitle({title, copy}: {title: string; copy: string}) { return <header className="config-section-title"><h2>{title}</h2><p>{copy}</p></header>; }
function ConfigEmpty({text}: {text: string}) { return <div className="config-empty">{text}</div>; }
