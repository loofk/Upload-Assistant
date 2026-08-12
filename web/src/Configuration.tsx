import {FormEvent, ReactNode, useCallback, useEffect, useState} from "react";
import {ApiClient} from "./api";
import LLMProvidersPanel from "./LLMProvidersPanel";
import RulesPanel from "./RulesPanel";
import {Drawer, InfoTip, ResourceHeader, SwitchField} from "./ui";
import type {AdapterCatalogEnvelope, Downloader, DownloaderAdapterCapability, ImageHost, LegacyMigrationPreview, LegacyMigrationRecord, MediaManager, MetadataProvider, NotificationChannel, PathMapping, ScreenshotProfile, SiteSummary} from "./types";

export type ConfigTab = "capabilities" | "downloaders" | "image-hosts" | "notifications" | "media-managers" | "metadata-providers" | "screenshots" | "ai-models" | "rules" | "migration";

const configGroups: Array<{label: string; tabs: ConfigTab[]}> = [
  {label: "集成", tabs: ["downloaders", "image-hosts", "notifications", "media-managers", "metadata-providers", "screenshots"]},
  {label: "AI 与规则", tabs: ["ai-models", "rules"]},
  {label: "高级", tabs: ["capabilities", "migration"]},
];

export default function Configuration({client, onError, tab, onTabChange}: {client: ApiClient; onError: (reason: unknown) => void; tab: ConfigTab; onTabChange: (tab: ConfigTab) => void}) {
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

  const tabLabel = (value: ConfigTab) => value === "capabilities" ? `能力 ${catalog?.count ?? 0}` : value === "downloaders" ? `下载器 ${downloaders.length}` : value === "image-hosts" ? `图床 ${imageHosts.length}` : value === "notifications" ? `通知 ${notificationChannels.length}` : value === "media-managers" ? `媒体管理 ${mediaManagers.length}` : value === "metadata-providers" ? `元数据 ${metadataProviders.length}` : value === "screenshots" ? `截图 ${screenshots.length}` : value === "ai-models" ? "AI 模型" : value === "rules" ? `站点规则 ${sites.length}` : "旧配置迁移";

  return <main className="configuration-pane">
    <header className="config-tabs" aria-label="配置分类">
      <nav aria-label="配置项目">
        {configGroups.map((group) => <div className="config-tab-group" key={group.label} aria-label={group.label}>
          {group.tabs.map((value) => <button key={value} className={tab === value ? "active" : ""} aria-current={tab === value ? "page" : undefined} onClick={() => onTabChange(value)}>{tabLabel(value)}</button>)}
        </div>)}
      </nav>
      <button className="ghost compact config-refresh" onClick={() => void reload()} disabled={loading}>刷新</button>
    </header>
    <div className="configuration-content">
		  {tab === "capabilities" && <AdapterCatalogPanel catalog={catalog} />}
		  {tab === "downloaders" && <DownloadersPanel items={downloaders} adapters={downloaderAdapters} client={client} reload={reload} onError={onError} />}
      {tab === "image-hosts" && <ImageHostsPanel items={imageHosts} client={client} reload={reload} onError={onError} />}
      {tab === "notifications" && <NotificationChannelsPanel items={notificationChannels} client={client} reload={reload} onError={onError} />}
      {tab === "media-managers" && <MediaManagersPanel items={mediaManagers} client={client} reload={reload} onError={onError} />}
      {tab === "metadata-providers" && <MetadataProvidersPanel items={metadataProviders} client={client} reload={reload} onError={onError} />}
      {tab === "screenshots" && <ScreenshotsPanel items={screenshots} client={client} reload={reload} onError={onError} />}
      {tab === "ai-models" && <LLMProvidersPanel client={client} onError={onError} />}
      {tab === "rules" && <RulesPanel sites={sites} catalog={catalog} client={client} reloadSites={reload} onError={onError} />}
      {tab === "migration" && <LegacyMigrationPanel client={client} />}
    </div>
  </main>;
}

function AdapterCatalogPanel({catalog}: {catalog: AdapterCatalogEnvelope | null}) {
	const [kind, setKind] = useState("");
	const [support, setSupport] = useState("");
	if (!catalog) return <ConfigEmpty text="能力契约尚未加载。" />;
	const kinds = [...new Set(catalog.adapters.map((item) => item.kind))].sort();
	const visible = catalog.adapters.filter((item) => (!kind || item.kind === kind) && (!support || (support === "runtime") === item.runtime_supported));
	return <section><ResourceHeader title="适配器能力" description="只读查看当前运行时支持的适配器。" action={<InfoTip label="能力目录说明"><span>operation、凭据字段和安全门禁来自本地能力契约，系统不会根据名称推断能力。</span><code>{catalog.catalog_version}</code><code>contract sha256: {catalog.catalog_sha256}</code></InfoTip>}/>
		<div className="catalog-filters"><label>类型<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="">全部类型</option>{kinds.map((value) => <option key={value}>{value}</option>)}</select></label><label>运行状态<select value={support} onChange={(event) => setSupport(event.target.value)}><option value="">全部状态</option><option value="runtime">运行时可用</option><option value="config">仅可配置</option></select></label></div>
		<div className="integration-grid">{visible.map((item) => <IntegrationCard key={item.id} title={item.site_code ? `${item.site_code} · ${item.display_name}` : item.display_name} type={adapterKindLabel(item.kind)} enabled={item.runtime_supported} health={item.runtime_supported ? "ready" : "config-only"} summary={item.runtime_supported ? "运行时可用" : item.unavailable_reason ?? "仅可配置"} technical={[`${item.kind} / ${item.adapter}`, ...(item.operations.length ? item.operations.map((operation) => `动作：${operation}`) : ["没有可调用动作"]), ...item.credential_fields.map((field) => `凭据：${field}`), ...item.safety_gates.map((gate) => `门禁：${gate}`), ...item.constraints]} />)}</div>
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
    <header className="migration-hero"><div><h2>Python 配置迁移</h2><p>只读取固定只读挂载中的 <code>config.py</code> 与中文站点 cookie；不启动 Python、不自动联网、不删除原文件。</p></div><button className="secondary" disabled={busy} onClick={() => void load()}>重新预览</button></header>
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
	const emptyForm = () => ({name: "default", adapter: "qbittorrent", enabled: true, networkClass: "unknown" as Downloader["network_class"], endpoint: "http://host.docker.internal:8080", username: "", password: "", apiKey: "", pathMappings: [{remote_path: "/downloads", local_path: "/downloads", priority: 100}] as PathMapping[]});
	const [form, setForm] = useState(emptyForm);
	const [editingName, setEditingName] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [custom, setCustom] = useState(false);
  const [busy, setBusy] = useState(false);
	const [probeStates, setProbeStates] = useState<Record<string, {status: "probing" | "ready" | "failed"; message: string}>>({});
	const selectedCapability = adapters.find((item) => item.adapter === form.adapter);
	const supportsCredential = (field: string) => selectedCapability?.credential_fields.includes(field) ?? false;
	const resetForm = () => { setEditingName(null); setForm(emptyForm()); setCustom(false); setEditorOpen(false); };
	const create = () => { setEditingName(null); setForm(emptyForm()); setCustom(false); setEditorOpen(true); };
	const edit = (item: Downloader) => {
		setEditingName(item.name);
		setForm({
			name: item.name, adapter: item.adapter, enabled: item.enabled, networkClass: item.network_class, endpoint: item.config.endpoint,
			username: "", password: "", apiKey: "",
			pathMappings: item.path_mappings.length ? item.path_mappings.map((mapping) => ({...mapping})) : [],
		});
		setCustom(item.config.endpoint !== "http://host.docker.internal:8080" || item.path_mappings.some((mapping) => mapping.remote_path !== "/downloads" || mapping.local_path !== "/downloads" || (mapping.priority ?? 100) !== 100));
		setEditorOpen(true);
	};
	const probe = async (item: Downloader) => {
		setProbeStates((current) => ({...current, [item.name]: {status: "probing", message: "正在连接下载器并读取版本信息…"}}));
		try {
			await client.probeDownloader(item.name);
			setProbeStates((current) => ({...current, [item.name]: {status: "ready", message: "连接成功，健康状态与审计证据已更新。"}}));
			await reload();
		} catch (reason) {
			const message = reason instanceof Error ? reason.message : "下载器探测失败。";
			setProbeStates((current) => ({...current, [item.name]: {status: "failed", message}}));
			onError(reason);
		}
	};
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
			const incompleteMapping = form.pathMappings.find((mapping) => !mapping.remote_path.trim() || !mapping.local_path.trim());
			if (incompleteMapping) throw new Error("每条路径映射都必须同时填写远程路径和容器路径。可删除不需要的空行。");
      await client.putDownloader(form.name, {
				adapter: form.adapter, enabled: form.enabled, networkClass: form.networkClass, endpoint: form.endpoint, credentials,
			pathMappings: form.pathMappings,
      });
		resetForm();
      await reload();
    } catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <section><ResourceHeader title="远程下载器" description="管理下载器连接、网络类型和路径映射。" action={<button className="primary" onClick={create}>新增下载器</button>}/>
			<div className="integration-grid">{items.map((item) => { const probeState = probeStates[item.name]; return <IntegrationCard key={item.id} title={item.name} type={item.adapter_capability?.display_name ?? item.adapter} enabled={item.enabled} health={item.health_status} summary={item.network_class === "seedbox" ? "盒子" : item.network_class === "home" ? "家宽" : "网络类型未标记"} technical={[item.config.endpoint, ...item.path_mappings.map((mapping) => `${mapping.remote_path} → ${mapping.local_path}`), ...(item.adapter_capability?.constraints ?? [])]} action={<div className="integration-actions"><div><button className="secondary" type="button" onClick={() => edit(item)}>编辑配置</button><button className="card-action" type="button" disabled={!item.enabled || !item.adapter_capability?.operations.probe || probeState?.status === "probing"} onClick={() => void probe(item)}>{probeState?.status === "probing" ? "探测中…" : "显式探测"}</button></div>{probeState && <p className={`probe-feedback ${probeState.status}`} role={probeState.status === "failed" ? "alert" : "status"}>{probeState.message}</p>}</div>} />; })}{!items.length && <ConfigEmpty text="尚未配置下载器。" />}</div>
	<ConfigForm open={editorOpen} onClose={resetForm} title={editingName ? `编辑下载器 · ${editingName}` : "添加下载器"} description="凭据只写入加密存储，留空会保留已有值。" onSubmit={submit} busy={busy}>
		<label>配置名称<input value={form.name} required readOnly={editingName !== null} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
		<label>适配器<select disabled={editingName !== null} value={form.adapter} onChange={(event) => { const adapter = event.target.value; const capability = adapters.find((item) => item.adapter === adapter); const endpoints: Record<string, string> = {qbittorrent: "http://host.docker.internal:8080", transmission: "http://host.docker.internal:9091/transmission/rpc", rtorrent: "http://host.docker.internal/RPC2", deluge: "http://host.docker.internal:8112/json"}; setForm({...form, adapter, endpoint: endpoints[adapter] ?? form.endpoint, enabled: capability?.runtime_supported ?? false, username: "", password: "", apiKey: ""}); }}>{adapters.map((item) => <option key={item.adapter} value={item.adapter}>{item.display_name}{item.runtime_supported ? "" : "（仅可禁用保存）"}</option>)}</select></label>
			<SwitchField checked={form.enabled} disabled={!adapters.find((item) => item.adapter === form.adapter)?.runtime_supported} onChange={(enabled) => setForm({...form, enabled})} label="启用下载器"/>
			<label>网络类型<select aria-label="网络类型" value={form.networkClass} onChange={(event) => setForm({...form, networkClass: event.target.value as Downloader["network_class"]})}><option value="unknown">未标记</option><option value="home">家宽</option><option value="seedbox">盒子 / SeedBox</option></select><small>由人工确认；系统不会根据名称或 IP 猜测。</small></label>
		{supportsCredential("username") && <label>用户名（与密码同时填写）<input autoComplete="off" value={form.username} onChange={(event) => setForm({...form, username: event.target.value})} /></label>}
		{supportsCredential("password") && <label>{supportsCredential("username") ? "密码（与用户名同时填写）" : editingName ? "Web 密码（留空则保留）" : "Web 密码（新建必填）"}<input type="password" autoComplete="new-password" value={form.password} onChange={(event) => setForm({...form, password: event.target.value})} /></label>}
		{supportsCredential("api_key") && <label className="full">API Key（可选、留空则保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>}
		<div className="custom-settings full"><SwitchField checked={custom} onChange={setCustom} label="自定义连接" description="调整服务地址或路径映射。默认配置适用于常见的本地部署。"/>{Boolean(selectedCapability?.constraints?.length) && <InfoTip label="适配器限制">{selectedCapability?.constraints?.map((item) => <span key={item}>{item}</span>)}</InfoTip>}</div>
		{custom && <><label className="full">服务地址<input type="url" value={form.endpoint} required onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>
		<div className="path-mapping-editor full">
			<header>
				<div className="path-mapping-heading"><div><strong>路径映射</strong><PathMappingHelp /></div><span>下载器保存路径 → 容器挂载路径</span></div>
				<button type="button" className="secondary" onClick={() => setForm({...form, pathMappings: [...form.pathMappings, {remote_path: "", local_path: "/downloads", priority: 100}]})}>添加路径</button>
			</header>
			{form.pathMappings.map((mapping, index) => <div className="path-mapping-row" key={index}>
				<label>下载器路径<input aria-label={`远程路径 ${index + 1}`} placeholder="/mnt/media/downloads" value={mapping.remote_path} onChange={(event) => setForm({...form, pathMappings: form.pathMappings.map((item, itemIndex) => itemIndex === index ? {...item, remote_path: event.target.value} : item)})} /><small>qBittorrent 返回</small></label>
				<label>容器路径<input aria-label={`容器路径 ${index + 1}`} placeholder="/downloads" value={mapping.local_path} onChange={(event) => setForm({...form, pathMappings: form.pathMappings.map((item, itemIndex) => itemIndex === index ? {...item, local_path: event.target.value} : item)})} /><small>Docker 内可见</small></label>
				<label>优先级<input aria-label={`路径优先级 ${index + 1}`} type="number" value={mapping.priority ?? 100} onChange={(event) => setForm({...form, pathMappings: form.pathMappings.map((item, itemIndex) => itemIndex === index ? {...item, priority: Number(event.target.value)} : item)})} /><small>越大越优先</small></label>
				<button type="button" className="danger" aria-label={`删除路径映射 ${index + 1}`} onClick={() => setForm({...form, pathMappings: form.pathMappings.filter((_, itemIndex) => itemIndex !== index)})}>删除</button>
			</div>)}
		</div></>}
  </ConfigForm></section>;
}

type ImageHostAdapter = "imgbb" | "ptpimg" | "imgbox" | "pixhost";

const imageHostDefinitions: Record<ImageHostAdapter, {label: string; endpoint: string; requiresAPIKey: boolean}> = {
  imgbb: {label: "ImgBB", endpoint: "https://api.imgbb.com/1/upload", requiresAPIKey: true},
  ptpimg: {label: "PTPimg", endpoint: "https://ptpimg.me/upload.php", requiresAPIKey: true},
  imgbox: {label: "Imgbox", endpoint: "https://imgbox.com", requiresAPIKey: false},
  pixhost: {label: "Pixhost", endpoint: "https://api.pixhost.to/images", requiresAPIKey: false},
};

const imageHostDefinition = (adapter: string) => imageHostDefinitions[adapter as ImageHostAdapter];
const emptyImageHostForm = () => ({name: "default", adapter: "imgbb" as ImageHostAdapter, endpoint: imageHostDefinitions.imgbb.endpoint, apiKey: "", priority: 100, enabled: true});

export function ImageHostsPanel({items, client, reload, onError}: {items: ImageHost[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const emptyForm = emptyImageHostForm;
  const [form, setForm] = useState(emptyForm);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [custom, setCustom] = useState(false);
  const [busy, setBusy] = useState(false);
  const [probeStates, setProbeStates] = useState<Record<string, ProbeViewState>>({});
  const close = () => { setEditorOpen(false); setEditingName(null); setCustom(false); setForm(emptyForm()); };
  const create = () => { setEditingName(null); setCustom(false); setForm(emptyForm()); setEditorOpen(true); };
  const edit = (item: ImageHost) => { const definition = imageHostDefinition(item.adapter); setEditingName(item.name); setForm({name: item.name, adapter: item.adapter as ImageHostAdapter, endpoint: item.config.endpoint, apiKey: "", priority: item.priority, enabled: item.enabled}); setCustom(item.priority !== 100 || !definition || definition.endpoint !== item.config.endpoint); setEditorOpen(true); };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putImageHost(form.name, form); await reload(); close(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  const definition = imageHostDefinitions[form.adapter];
  const probe = async (item: ImageHost) => {
    if (!window.confirm(`将向 ${imageHostDefinition(item.adapter)?.label ?? item.adapter} 上传一张 100×100 测试图。远端可能保留这张图片，是否继续？`)) return;
    setProbeStates((current) => ({...current, [item.name]: {status: "probing", message: "正在上传测试图…"}}));
    try {
      await client.probeImageHost(item.name);
      setProbeStates((current) => ({...current, [item.name]: {status: "ready", message: "测试图上传成功，连接状态已更新。"}}));
      await reload();
    } catch (reason) {
      setProbeStates((current) => ({...current, [item.name]: {status: "failed", message: probeErrorMessage(reason, "图床测试失败。")}}));
      onError(reason);
    }
  };
  return <section><ResourceHeader title="独立图床" description="为截图选择上传服务；Imgbox 和 Pixhost 无需 API Key。" action={<button className="primary" onClick={create}>新增图床</button>}/>
    <div className="integration-grid">{items.map((item) => { const itemDefinition = imageHostDefinition(item.adapter); const probeState = probeStates[item.name]; return <IntegrationCard key={item.id} title={item.name} type={itemDefinition?.label ?? item.adapter} enabled={item.enabled} health={item.health_status} summary={`${itemDefinition?.requiresAPIKey === false ? "无需凭据 · " : ""}优先级 ${item.priority}`} technical={[item.config.endpoint, ...lastTestDetail(item.last_health_check_at)]} action={itemDefinition ? <ProbeActions state={probeState} edit={() => edit(item)} disabled={!item.enabled} busyLabel="测试中…" actionLabel="测试图床" run={() => void probe(item)} /> : undefined}/>; }) }{!items.length && <ConfigEmpty text="尚未配置图床。" />}</div>
  <ConfigForm open={editorOpen} onClose={close} title={editingName ? `编辑图床 · ${editingName}` : "新增图床"} description={definition.requiresAPIKey ? "API Key 留空会保留已有密钥。" : "该图床无需凭据，保存后即可供任务选择。"} onSubmit={submit} busy={busy} showSecretTip={definition.requiresAPIKey}>
    <label>配置名称<input required readOnly={editingName !== null} value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>图床服务<select disabled={editingName !== null} value={form.adapter} onChange={(event) => { const adapter = event.target.value as ImageHostAdapter; setForm({...form, adapter, endpoint: imageHostDefinitions[adapter].endpoint, apiKey: ""}); }}><option value="imgbb">ImgBB</option><option value="ptpimg">PTPimg</option><option value="imgbox">Imgbox（无需 Key）</option><option value="pixhost">Pixhost（无需 Key）</option></select></label>
    <SwitchField checked={form.enabled} onChange={(enabled) => setForm({...form, enabled})} label="启用图床"/>
    {definition.requiresAPIKey && <label>API Key（留空则保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>}
    <div className="custom-settings full"><SwitchField checked={custom} onChange={setCustom} label="自定义设置" description={form.adapter === "imgbox" ? "调整优先级。" : form.adapter === "pixhost" ? "选择服务节点或调整优先级。" : "调整服务地址或优先级。"}/></div>
    {custom && <>
      {form.adapter === "pixhost" ? <label className="full">服务节点<select value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})}><option value="https://api.pixhost.to/images">pixhost.to</option><option value="https://api.pixhost.cc/images">pixhost.cc</option><option value="https://api.pixho.st/images">pixho.st</option></select></label> : definition.requiresAPIKey ? <label className="full">服务地址<input type="url" required value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label> : null}
      <label>优先级<input type="number" value={form.priority} onChange={(event) => setForm({...form, priority: Number(event.target.value)})} /></label>
    </>}
  </ConfigForm></section>;
}

export function NotificationChannelsPanel({items, client, reload, onError}: {items: NotificationChannel[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const eventOptions = [
    ["job.created", "任务创建"], ["job.completed", "任务完成"], ["step.blocked", "需要人工处理"],
    ["step.failed", "执行失败"], ["step.deferred", "限频延后"], ["job.paused", "任务暂停"],
    ["job.resumed", "任务继续"], ["job.cancelled", "任务取消"],
    ["target_package.revision_requested", "发布内容重生成"], ["job.reconciliation_acknowledged", "远程结果核对"],
  ] as const;
  const emptyForm = (): {name: string; adapter: NotificationChannel["adapter"]; enabled: boolean; webhookURL: string; botToken: string; chatID: string; eventTypes: string[]} => ({name: "telegram-main", adapter: "telegram_bot", enabled: true, webhookURL: "", botToken: "", chatID: "", eventTypes: eventOptions.map(([value]) => value)});
  const [form, setForm] = useState(emptyForm);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [customEvents, setCustomEvents] = useState(false);
  const [busy, setBusy] = useState(false);
  const [probeStates, setProbeStates] = useState<Record<string, ProbeViewState>>({});
  const close = () => { setEditorOpen(false); setEditingName(null); setCustomEvents(false); setForm(emptyForm()); };
  const create = () => { setEditingName(null); setCustomEvents(false); setForm(emptyForm()); setEditorOpen(true); };
  const edit = (item: NotificationChannel) => { const eventTypes = item.config.event_types ?? []; setEditingName(item.name); setForm({name: item.name, adapter: item.adapter, enabled: item.enabled, webhookURL: "", botToken: "", chatID: "", eventTypes}); setCustomEvents(eventTypes.length !== eventOptions.length); setEditorOpen(true); };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putNotificationChannel(form.name, form); await reload(); close(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  const probe = async (item: NotificationChannel) => {
    if (!window.confirm(`将通过“${item.name}”发送一条真实测试消息。测试只执行一次，不会自动重试，是否继续？`)) return;
    setProbeStates((current) => ({...current, [item.name]: {status: "probing", message: "正在发送测试消息…"}}));
    try {
      await client.probeNotificationChannel(item.name);
      setProbeStates((current) => ({...current, [item.name]: {status: "ready", message: "测试消息已送达，连接状态已更新。"}}));
      await reload();
    } catch (reason) {
      setProbeStates((current) => ({...current, [item.name]: {status: "failed", message: probeErrorMessage(reason, "测试消息发送失败。")}}));
      onError(reason);
    }
  };
  return <section><ResourceHeader title="通知渠道" description="接收任务状态和每日候选结果。" action={<button className="primary" onClick={create}>新增通知渠道</button>}/>
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={notificationAdapterLabel(item.adapter)} enabled={item.enabled} health={item.health_status} summary={`${item.config.event_types?.length ?? 0} 类事件`} technical={[`超时 ${item.config.timeout_seconds ?? 15}s`, ...item.credential_fields.map((field) => `${field} 已配置`), ...lastTestDetail(item.last_health_check_at)]} action={<ProbeActions state={probeStates[item.name]} edit={() => edit(item)} disabled={!item.enabled} busyLabel="发送中…" actionLabel="发送测试消息" run={() => void probe(item)} />}/>) }{!items.length && <ConfigEmpty text="尚未配置通知渠道；任务与候选结果仍会保留在本地。" />}</div>
  <ConfigForm open={editorOpen} onClose={close} title={editingName ? `编辑通知 · ${editingName}` : "新增通知渠道"} description="凭据留空会保留已有值。" onSubmit={submit} busy={busy}>
    <label>渠道名称<input required readOnly={editingName !== null} value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>渠道类型<select disabled={editingName !== null} value={form.adapter} onChange={(event) => { const adapter = event.target.value as NotificationChannel["adapter"]; setForm({...form, adapter, name: `${adapter.replace(/_.*/, "")}-main`}); }}><option value="telegram_bot">Telegram Bot</option><option value="wecom_bot">企业微信群机器人</option><option value="feishu_bot">飞书群机器人</option><option value="discord_webhook">Discord Webhook</option></select></label>
    <SwitchField checked={form.enabled} onChange={(enabled) => setForm({...form, enabled})} label="启用通知"/>
    {form.adapter === "telegram_bot" ? <><label className="full">Bot Token（新建必填，更新时两项均留空可保留）<input type="password" autoComplete="new-password" value={form.botToken} onChange={(event) => setForm({...form, botToken: event.target.value})} /></label><label className="full">Chat ID<input type="password" autoComplete="new-password" value={form.chatID} onChange={(event) => setForm({...form, chatID: event.target.value})} /></label></> : <label className="full">Webhook URL（新建必填，更新留空保留）<input type="password" autoComplete="new-password" value={form.webhookURL} onChange={(event) => setForm({...form, webhookURL: event.target.value})} /></label>}
    <div className="custom-settings full"><SwitchField checked={customEvents} onChange={(enabled) => { setCustomEvents(enabled); if (!enabled) setForm({...form, eventTypes: eventOptions.map(([value]) => value)}); }} label="自定义通知事件" description="默认接收全部关键状态；仅在需要减少通知时调整。"/></div>
    {customEvents && <fieldset className="notification-events full"><legend>接收事件</legend>{eventOptions.map(([value, label]) => <label key={value}><input type="checkbox" checked={form.eventTypes.includes(value)} onChange={(event) => setForm({...form, eventTypes: event.target.checked ? [...form.eventTypes, value] : form.eventTypes.filter((item) => item !== value)})} />{label}</label>)}</fieldset>}
  </ConfigForm></section>;
}

function MediaManagersPanel({items, client, reload, onError}: {items: MediaManager[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const emptyForm = () => ({name: "sonarr-main", adapter: "sonarr" as "sonarr" | "radarr", enabled: true, endpoint: "http://host.docker.internal:8989", apiKey: ""});
  const [form, setForm] = useState(emptyForm);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [custom, setCustom] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => { setEditorOpen(false); setEditingName(null); setCustom(false); setForm(emptyForm()); };
  const create = () => { setEditingName(null); setCustom(false); setForm(emptyForm()); setEditorOpen(true); };
  const edit = (item: MediaManager) => { const defaultEndpoint = item.adapter === "sonarr" ? "http://host.docker.internal:8989" : "http://host.docker.internal:7878"; setEditingName(item.name); setForm({name: item.name, adapter: item.adapter, enabled: item.enabled, endpoint: item.config.endpoint, apiKey: ""}); setCustom(item.config.endpoint !== defaultEndpoint); setEditorOpen(true); };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putMediaManager(form.name, form); await reload(); close(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  return <section><ResourceHeader title="媒体管理" description="连接 Sonarr 或 Radarr，探测只在明确点击后执行。" action={<button className="primary" onClick={create}>新增媒体管理</button>}/>
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter === "sonarr" ? "Sonarr" : "Radarr"} enabled={item.enabled} health={item.health_status} summary={item.credential_fields.includes("api_key") ? "凭据已配置" : "缺少 API Key"} technical={[item.config.endpoint]} action={<div className="integration-actions"><button className="secondary" onClick={() => edit(item)}>编辑配置</button><button className="card-action" disabled={!item.enabled} onClick={async () => { try { await client.probeMediaManager(item.name); await reload(); } catch (reason) { onError(reason); } }}>显式探测</button></div>} />)}{!items.length && <ConfigEmpty text="尚未配置 Sonarr/Radarr。" />}</div>
  <ConfigForm open={editorOpen} onClose={close} title={editingName ? `编辑媒体管理 · ${editingName}` : "新增媒体管理"} description="保存不会访问外部服务。" onSubmit={submit} busy={busy}>
    <label>实例名称<input required readOnly={editingName !== null} value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>适配器<select disabled={editingName !== null} value={form.adapter} onChange={(event) => { const adapter = event.target.value as "sonarr" | "radarr"; setForm({...form, adapter, endpoint: adapter === "sonarr" ? "http://host.docker.internal:8989" : "http://host.docker.internal:7878", name: `${adapter}-main`}); }}><option value="sonarr">Sonarr</option><option value="radarr">Radarr</option></select></label>
    <SwitchField checked={form.enabled} onChange={(enabled) => setForm({...form, enabled})} label="启用实例"/>
    <label className="full">API Key（新建必填，更新留空保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>
    <div className="custom-settings full"><SwitchField checked={custom} onChange={setCustom} label="自定义服务地址"/></div>
    {custom && <label className="full">服务地址<input type="url" required value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>}
  </ConfigForm></section>;
}

export function MetadataProvidersPanel({items, client, reload, onError}: {items: MetadataProvider[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const emptyForm = () => ({name: "tmdb-main", adapter: "tmdb" as "tmdb" | "ptgen", enabled: true, endpoint: "https://api.themoviedb.org", apiKey: ""});
  const [form, setForm] = useState(emptyForm);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [probeStates, setProbeStates] = useState<Record<string, ProbeViewState>>({});
  const close = () => { setEditorOpen(false); setEditingName(null); setForm(emptyForm()); };
  const create = () => { setEditingName(null); setForm(emptyForm()); setEditorOpen(true); };
  const edit = (item: MetadataProvider) => { setEditingName(item.name); setForm({name: item.name, adapter: item.adapter, enabled: item.enabled, endpoint: item.config.endpoint, apiKey: ""}); setEditorOpen(true); };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await client.putMetadataProvider(form.name, form); await reload(); close(); }
    catch (reason) { onError(reason); } finally { setBusy(false); }
  };
  const probe = async (item: MetadataProvider) => {
    setProbeStates((current) => ({...current, [item.name]: {status: "probing", message: "正在验证查询契约…"}}));
    try {
      await client.probeMetadataProvider(item.name);
      setProbeStates((current) => ({...current, [item.name]: {status: "ready", message: "测试查询成功，连接状态已更新。"}}));
      await reload();
    } catch (reason) {
      setProbeStates((current) => ({...current, [item.name]: {status: "failed", message: probeErrorMessage(reason, "元数据服务测试失败。")}}));
      onError(reason);
    }
  };
  return <section><ResourceHeader title="元数据服务" description="配置 TMDb 或自建 PTGen。" action={<button className="primary" onClick={create}>新增元数据服务</button>}/>
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={item.adapter === "tmdb" ? "TMDb" : "PTGen"} enabled={item.enabled} health={item.health_status} summary={item.credential_fields.includes("api_key") ? "凭据已配置" : item.adapter === "ptgen" ? "无凭据" : "缺少 API Key"} technical={[item.config.endpoint, ...lastTestDetail(item.last_health_check_at)]} action={<ProbeActions state={probeStates[item.name]} edit={() => edit(item)} disabled={!item.enabled} busyLabel="测试中…" actionLabel="测试查询" run={() => void probe(item)} />}/>) }{!items.length && <ConfigEmpty text="尚未配置元数据服务。" />}</div>
  <ConfigForm open={editorOpen} onClose={close} title={editingName ? `编辑元数据 · ${editingName}` : "新增元数据服务"} description="保存配置不会发起网络请求。" onSubmit={submit} busy={busy}>
    <label>配置名称<input required readOnly={editingName !== null} value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label>
    <label>适配器<select disabled={editingName !== null} value={form.adapter} onChange={(event) => { const adapter = event.target.value as "tmdb" | "ptgen"; setForm({...form, adapter, endpoint: adapter === "tmdb" ? "https://api.themoviedb.org" : "", name: adapter === "tmdb" ? "tmdb-main" : "ptgen-main"}); }}><option value="tmdb">TMDb</option><option value="ptgen">PTGen</option></select></label>
    <SwitchField checked={form.enabled} onChange={(enabled) => setForm({...form, enabled})} label="启用服务"/>
    {form.adapter === "ptgen" && <label className="full">PTGen API 地址<input type="url" required placeholder="https://your-ptgen.example/api" value={form.endpoint} onChange={(event) => setForm({...form, endpoint: event.target.value})} /></label>}
    <label className="full">API Key（TMDb 新建必填；PTGen 可选；更新留空保留）<input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({...form, apiKey: event.target.value})} /></label>
  </ConfigForm></section>;
}

function ScreenshotsPanel({items, client, reload, onError}: {items: ScreenshotProfile[]; client: ApiClient; reload: () => Promise<void>; onError: (reason: unknown) => void}) {
  const emptyForm = () => ({name: "default", count: 6, format: "png", width: 0, quality: 90, startPercent: .1, endPercent: .9});
  const [form, setForm] = useState(emptyForm);
  const [editingRevision, setEditingRevision] = useState<number | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [custom, setCustom] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => { setEditorOpen(false); setEditingRevision(null); setCustom(false); setForm(emptyForm()); };
  const create = () => { setEditingRevision(null); setCustom(false); setForm(emptyForm()); setEditorOpen(true); };
  const edit = (item: ScreenshotProfile) => { const next = {name: item.name, count: Number(item.config.count ?? 6), format: String(item.config.format ?? "png"), width: Number(item.config.width ?? 0), quality: Number(item.config.quality ?? 90), startPercent: Number(item.config.start_percent ?? .1), endPercent: Number(item.config.end_percent ?? .9)}; setForm(next); setEditingRevision(item.revision); setCustom(next.width !== 0 || next.quality !== 90 || next.startPercent !== .1 || next.endPercent !== .9); setEditorOpen(true); };
  const submit = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { await client.createScreenshotProfile(form); await reload(); close(); } catch (reason) { onError(reason); } finally { setBusy(false); } };
  return <section><ResourceHeader title="截图策略" description="保存同名策略会创建不可变的新版本。" action={<button className="primary" onClick={create}>新增截图策略</button>}/>
    <div className="integration-grid">{items.map((item) => <IntegrationCard key={item.id} title={item.name} type={`r${item.revision} · ${String(item.config.format ?? "png").toUpperCase()}`} enabled={item.enabled} health="immutable" summary={`${String(item.config.count ?? 0)} 张截图`} technical={[`${String(item.config.width ?? 0)} px`, `质量 ${String(item.config.quality ?? 90)}`, `${String(item.config.start_percent ?? 0)}–${String(item.config.end_percent ?? 1)} 时段`]} action={<button className="secondary" onClick={() => edit(item)}>创建新版本</button>}/>) }{!items.length && <ConfigEmpty text="尚未配置截图策略。" />}</div>
  <ConfigForm open={editorOpen} onClose={close} title={editingRevision ? `基于 r${editingRevision} 创建新版本` : "新增截图策略"} description="历史任务继续绑定旧版本。" onSubmit={submit} busy={busy}>
    <label>策略名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label><label>格式<select value={form.format} onChange={(event) => setForm({...form, format: event.target.value})}><option>png</option><option>jpg</option><option>webp</option></select></label>
    <label>截图数量<input type="number" min="1" max="20" value={form.count} onChange={(event) => setForm({...form, count: Number(event.target.value)})} /></label>
    <div className="custom-settings full"><SwitchField checked={custom} onChange={setCustom} label="自定义截图参数" description="默认保留原始宽度、90% 质量，并避开片头片尾。"/></div>
    {custom && <><label>宽度<select value={form.width} onChange={(event) => setForm({...form, width: Number(event.target.value)})}><option value="0">保持原始</option><option value="1920">1920 px</option><option value="1280">1280 px</option></select></label><label>质量<select value={form.quality} onChange={(event) => setForm({...form, quality: Number(event.target.value)})}><option value="90">高（90）</option><option value="80">标准（80）</option><option value="70">节省空间（70）</option></select></label><label>起始位置<select value={form.startPercent} onChange={(event) => setForm({...form, startPercent: Number(event.target.value)})}><option value="0.05">5%</option><option value="0.1">10%</option><option value="0.15">15%</option></select></label><label>结束位置<select value={form.endPercent} onChange={(event) => setForm({...form, endPercent: Number(event.target.value)})}><option value="0.85">85%</option><option value="0.9">90%</option><option value="0.95">95%</option></select></label></>}
  </ConfigForm></section>;
}

function IntegrationCard({title, type, enabled, health, summary, technical = [], action}: {title: string; type: string; enabled: boolean; health: string; summary: string; technical?: string[]; action?: ReactNode}) {
  const state = !enabled ? "disabled" : health === "failed" ? "failed" : health === "ready" || health === "immutable" ? "ready" : "unknown";
  return <article className="integration-card"><header><div><strong>{title}</strong><span>{type}</span></div><span className={`status-pill status-${state}`}>{!enabled ? "已停用" : healthStatusLabel(health)}</span></header><p>{summary}</p>{technical.length > 0 && <details className="integration-technical"><summary>技术详情</summary><div>{technical.map((detail) => <span key={detail}>{detail}</span>)}</div></details>}{action && <footer>{action}</footer>}</article>;
}

type ProbeViewState = {status: "probing" | "ready" | "failed"; message: string};

function ProbeActions({state, edit, disabled, busyLabel, actionLabel, run}: {state?: ProbeViewState; edit: () => void; disabled: boolean; busyLabel: string; actionLabel: string; run: () => void}) {
  return <div className="integration-actions"><div><button className="secondary" type="button" onClick={edit}>编辑配置</button><button className="card-action" type="button" disabled={disabled || state?.status === "probing"} onClick={run}>{state?.status === "probing" ? busyLabel : actionLabel}</button></div>{state && <p className={`probe-feedback ${state.status}`} role={state.status === "failed" ? "alert" : "status"}>{state.message}</p>}</div>;
}

function probeErrorMessage(reason: unknown, fallback: string): string {
  if (!(reason instanceof Error)) return fallback;
  return reason.message.trim() || fallback;
}

function lastTestDetail(value?: string): string[] {
  return value ? [`最近测试 ${new Date(value).toLocaleString("zh-CN")}`] : [];
}

function PathMappingHelp() {
	return <span className="path-help"><button type="button" className="info-trigger" aria-label="路径映射说明" aria-describedby="path-mapping-tooltip"><svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="10" cy="10" r="8" /><path d="M10 9v5M10 6.2v.1" /></svg></button><span className="path-help-tooltip" id="path-mapping-tooltip" role="tooltip"><strong>两边必须指向同一批文件</strong><span>下载器路径来自 Web API；容器路径来自 Docker 目录挂载。这里只转换路径前缀，不复制文件。</span><code>/downloads → /downloads</code><small>两边路径相同</small><code>/mnt/media/downloads → /downloads</code><small>盒子目录挂载到容器</small><span>规则越具体、优先级越高，越先匹配。</span></span></span>;
}

function ConfigForm({open, onClose, title, description, onSubmit, busy, children, showSecretTip = true}: {open: boolean; onClose: () => void; title: string; description?: string; onSubmit: (event: FormEvent) => void; busy: boolean; children: ReactNode; showSecretTip?: boolean}) {
  const [dirty, setDirty] = useState(false);
  useEffect(() => { if (open) setDirty(false); }, [open, title]);
  return <Drawer open={open} onClose={onClose} title={title} description={description} dirty={dirty}>
    <form className="config-drawer-form" onSubmit={onSubmit} onChangeCapture={() => setDirty(true)}>{children}<footer className="full">{showSecretTip && <InfoTip label="凭据保存说明">Secret 字段使用主密钥加密，API 和日志只显示是否已配置。</InfoTip>}<button className="primary" disabled={busy}>{busy ? "保存中…" : "保存配置"}</button></footer></form>
  </Drawer>;
}

function ConfigSectionTitle({title, copy}: {title: string; copy: string}) { return <header className="config-section-title"><h2>{title}</h2><p>{copy}</p></header>; }
function ConfigEmpty({text}: {text: string}) { return <div className="config-empty">{text}</div>; }
function healthStatusLabel(value: string) { if (value === "ready") return "连接正常"; if (value === "failed") return "验证失败"; if (value === "immutable") return "不可变版本"; if (value === "catalog_ready") return "目录可用"; return "未测试"; }
function notificationAdapterLabel(value: NotificationChannel["adapter"]) { return value === "telegram_bot" ? "Telegram Bot" : value === "wecom_bot" ? "企业微信机器人" : value === "feishu_bot" ? "飞书机器人" : "Discord Webhook"; }
function adapterKindLabel(value: string) { const labels: Record<string, string> = {downloader: "下载器", image_host: "图床", media_analyzer: "媒体分析", media_manager: "媒体管理", metadata_provider: "元数据", notification_channel: "通知", screenshot_engine: "截图", site: "站点", torrent_maker: "制种"}; return labels[value] ?? value; }
