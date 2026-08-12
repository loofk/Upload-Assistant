import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {ApiClient} from "./api";
import type {
	Downloader,
	DownloaderDashboardSnapshot,
	DownloaderDashboardTorrent,
	DownloaderTorrentFilesEvidence,
	DownloaderTorrentGroup,
} from "./types";

type DashboardFilter = "all" | DownloaderTorrentGroup | "active";

const filterLabels: Array<{value: DashboardFilter; label: string}> = [
	{value: "all", label: "全部"},
	{value: "downloading", label: "下载中"},
	{value: "seeding", label: "做种中"},
	{value: "active", label: "有流量"},
	{value: "paused", label: "已暂停"},
	{value: "checking", label: "校验中"},
	{value: "error", label: "异常"},
];

const stateLabels: Record<string, string> = {
	downloading: "下载中", seeding: "做种中", paused: "已暂停", checking: "校验中", error: "异常", completed: "已完成",
};

export default function DownloaderDashboard({client, onError, onOpenConfiguration}: {
	client: ApiClient;
	onError: (reason: unknown) => void;
	onOpenConfiguration: () => void;
}) {
	const [downloaders, setDownloaders] = useState<Downloader[]>([]);
	const [selectedName, setSelectedName] = useState("");
	const [snapshot, setSnapshot] = useState<DownloaderDashboardSnapshot | null>(null);
	const [filter, setFilter] = useState<DashboardFilter>("all");
	const [query, setQuery] = useState("");
	const [offset, setOffset] = useState(0);
	const [autoRefresh, setAutoRefresh] = useState(true);
	const [loading, setLoading] = useState(true);
	const [refreshing, setRefreshing] = useState(false);
	const [selectedTorrent, setSelectedTorrent] = useState<DownloaderDashboardTorrent | null>(null);
	const [files, setFiles] = useState<DownloaderTorrentFilesEvidence | null>(null);
	const [filesLoading, setFilesLoading] = useState(false);
	const requestSequence = useRef(0);

	useEffect(() => {
		let alive = true;
		setLoading(true);
		void client.listDownloaders().then((items) => {
			if (!alive) return;
			const available = items.filter((item) => item.enabled && item.adapter_capability?.operations.list_torrents);
			setDownloaders(available);
			setSelectedName((current) => available.some((item) => item.name === current) ? current : available[0]?.name ?? "");
		}).catch(onError).finally(() => { if (alive) setLoading(false); });
		return () => { alive = false; };
	}, [client, onError]);

	const loadSnapshot = useCallback(async (quiet = false) => {
		if (!selectedName) return;
		const sequence = ++requestSequence.current;
		if (!quiet) setRefreshing(true);
		try {
			const next = await client.getDownloaderSnapshot(selectedName, {filter, query: query.trim(), offset, limit: 100});
			if (sequence !== requestSequence.current) return;
			setSnapshot(next);
			setSelectedTorrent((current) => current ? next.torrents.find((item) => item.hash === current.hash) ?? current : null);
		} catch (reason) {
			if (sequence === requestSequence.current) onError(reason);
		} finally {
			if (!quiet && sequence === requestSequence.current) setRefreshing(false);
		}
	}, [client, filter, offset, onError, query, selectedName]);

	useEffect(() => {
		setSnapshot(null);
		setSelectedTorrent(null);
		setFiles(null);
		if (!selectedName) return;
		const timer = window.setTimeout(() => void loadSnapshot(), 220);
		return () => window.clearTimeout(timer);
	}, [selectedName, filter, query, offset, loadSnapshot]);

	useEffect(() => {
		if (!autoRefresh || !selectedName) return;
		const timer = window.setInterval(() => {
			if (document.visibilityState === "visible") void loadSnapshot(true);
		}, 5000);
		return () => window.clearInterval(timer);
	}, [autoRefresh, loadSnapshot, selectedName]);

	useEffect(() => {
		if (!selectedTorrent || !selectedName) return;
		let alive = true;
		setFiles(null);
		setFilesLoading(true);
		void client.getDownloaderTorrentFiles(selectedName, selectedTorrent.hash).then((result) => {
			if (alive) setFiles(result);
		}).catch(onError).finally(() => { if (alive) setFilesLoading(false); });
		return () => { alive = false; };
	}, [client, onError, selectedName, selectedTorrent?.hash]); // eslint-disable-line react-hooks/exhaustive-deps

	useEffect(() => {
		if (!selectedTorrent) return;
		const close = (event: KeyboardEvent) => { if (event.key === "Escape") setSelectedTorrent(null); };
		window.addEventListener("keydown", close);
		return () => window.removeEventListener("keydown", close);
	}, [selectedTorrent]);

	const selectedDownloader = downloaders.find((item) => item.name === selectedName);
	const filterCounts = useMemo(() => {
		const summary = snapshot?.summary;
		return {
			all: summary?.total ?? 0,
			downloading: summary?.downloading ?? 0,
			seeding: summary?.seeding ?? 0,
			active: summary?.active ?? 0,
			paused: summary?.paused ?? 0,
			checking: summary?.checking ?? 0,
			error: summary?.errors ?? 0,
			completed: 0,
		};
	}, [snapshot]);

	if (!loading && downloaders.length === 0) {
		return <section className="downloader-dashboard empty"><strong>还没有可读取的下载器</strong><p>先启用一个支持任务列表的下载器，再回到这里查看传输状态。</p><button className="primary" onClick={onOpenConfiguration}>配置下载器</button></section>;
	}

	return <section className="downloader-dashboard" aria-busy={loading || refreshing}>
		<header className="downloader-commandbar">
			<label className="downloader-picker"><span>下载器</span><select value={selectedName} onChange={(event) => { setSelectedName(event.target.value); setOffset(0); }} disabled={downloaders.length < 2}>{downloaders.map((item) => <option key={item.id} value={item.name}>{item.name} · {item.adapter_capability?.display_name ?? item.adapter}</option>)}</select></label>
			<div className="downloader-connection"><i className={snapshot ? "ready" : ""} /><div><strong>{selectedDownloader?.name ?? "正在读取"}</strong><span>{selectedDownloader ? networkLabel(selectedDownloader.network_class) : ""}{snapshot ? ` · ${formatClock(snapshot.fetched_at)} 更新` : ""}</span></div></div>
			<label className="dashboard-auto-refresh"><input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} /><span>自动刷新</span></label>
			<button className="secondary" type="button" disabled={!selectedName || refreshing} onClick={() => void loadSnapshot()}>{refreshing ? "刷新中…" : "刷新"}</button>
		</header>

		<div className="transfer-rail" aria-label="实时传输状态">
			<div className="transfer-speed download"><span>下载</span><strong>↓ {formatRate(snapshot?.summary.download_speed ?? 0)}</strong></div>
			<div className="transfer-speed upload"><span>上传</span><strong>↑ {formatRate(snapshot?.summary.upload_speed ?? 0)}</strong></div>
			<div className="transfer-count"><span>任务</span><strong>{snapshot?.summary.total ?? "—"}</strong></div>
			<div className="transfer-count"><span>活动</span><strong>{snapshot?.summary.active ?? "—"}</strong></div>
			<div className="transfer-count"><span>异常</span><strong className={snapshot?.summary.errors ? "danger-text" : ""}>{snapshot?.summary.errors ?? "—"}</strong></div>
		</div>

		<div className="downloader-list-toolbar">
			<div className="torrent-filter-tabs" role="group" aria-label="任务状态筛选">{filterLabels.map((item) => <button type="button" key={item.value} className={filter === item.value ? "active" : ""} aria-pressed={filter === item.value} onClick={() => { setFilter(item.value); setOffset(0); }}>{item.label}<span>{filterCounts[item.value]}</span></button>)}</div>
			<label className="torrent-search"><span className="visually-hidden">搜索任务</span><input type="search" value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} placeholder="搜索名称、分类或标签" /></label>
		</div>

		<div className="torrent-table-wrap">
			<table className="torrent-table">
				<thead><tr><th>任务</th><th>状态</th><th>进度</th><th>大小</th><th>下载</th><th>上传</th><th>分享率</th></tr></thead>
				<tbody>{snapshot?.torrents.map((torrent) => <tr key={torrent.hash} tabIndex={0} onClick={() => setSelectedTorrent(torrent)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedTorrent(torrent); } }}>
					<td><strong title={torrent.name}>{torrent.name}</strong><span>{[torrent.category, torrent.tags].filter(Boolean).join(" · ") || shortHash(torrent.hash)}</span></td>
					<td><span className={`torrent-state ${torrent.state_group}`}>{stateLabels[torrent.state_group]}</span></td>
					<td><div className="torrent-progress"><span><i style={{width: `${clampProgress(torrent.progress)}%`}} /></span><b>{formatProgress(torrent.progress)}</b></div></td>
					<td>{formatBytes(torrent.total_size)}</td><td className="numeric">{formatRate(torrent.download_speed)}</td><td className="numeric">{formatRate(torrent.upload_speed)}</td><td className="numeric">{formatRatio(torrent.ratio)}</td>
				</tr>)}</tbody>
			</table>
			{!snapshot && <div className="dashboard-loading"><span /><strong>正在读取下载器</strong></div>}
			{snapshot && snapshot.torrents.length === 0 && <div className="empty compact-empty"><strong>没有匹配的任务</strong><p>更换状态或清除搜索条件后再看。</p></div>}
		</div>

		{snapshot && snapshot.filtered_total > 0 && <footer className="torrent-pagination"><span>第 {snapshot.offset + 1}–{Math.min(snapshot.offset + snapshot.torrents.length, snapshot.filtered_total)} 条，共 {snapshot.filtered_total} 条</span><div><button className="secondary compact" disabled={snapshot.offset === 0} onClick={() => setOffset(Math.max(0, snapshot.offset - snapshot.limit))}>上一页</button><button className="secondary compact" disabled={!snapshot.has_more} onClick={() => setOffset(snapshot.offset + snapshot.limit)}>下一页</button></div></footer>}

		{selectedTorrent && <TorrentDrawer torrent={selectedTorrent} files={files} loading={filesLoading} onClose={() => setSelectedTorrent(null)} />}
	</section>;
}

function TorrentDrawer({torrent, files, loading, onClose}: {torrent: DownloaderDashboardTorrent; files: DownloaderTorrentFilesEvidence | null; loading: boolean; onClose: () => void}) {
	return <><button className="drawer-scrim" type="button" aria-label="关闭任务详情" onClick={onClose} /><aside className="torrent-drawer" role="dialog" aria-modal="true" aria-labelledby="torrent-detail-title">
		<header><div><span className={`torrent-state ${torrent.state_group}`}>{stateLabels[torrent.state_group]}</span><h2 id="torrent-detail-title">{torrent.name}</h2></div><button className="icon-button" type="button" aria-label="关闭任务详情" onClick={onClose}>×</button></header>
		<div className="torrent-drawer-content">
			<section className="torrent-detail-progress"><div><strong>{formatProgress(torrent.progress)}</strong><span>{formatBytes(torrent.amount_left)} 待下载</span></div><span><i style={{width: `${clampProgress(torrent.progress)}%`}} /></span></section>
			<dl className="torrent-detail-grid">
				<div><dt>下载速度</dt><dd>↓ {formatRate(torrent.download_speed)}</dd></div><div><dt>上传速度</dt><dd>↑ {formatRate(torrent.upload_speed)}</dd></div>
				<div><dt>已下载</dt><dd>{formatBytes(torrent.downloaded)}</dd></div><div><dt>已上传</dt><dd>{formatBytes(torrent.uploaded)}</dd></div>
				<div><dt>分享率</dt><dd>{formatRatio(torrent.ratio)}</dd></div><div><dt>活动时间</dt><dd>{formatDuration(torrent.time_active)}</dd></div>
			</dl>
			{(torrent.category || torrent.tags) && <section className="torrent-labels"><h3>分类与标签</h3><div>{torrent.category && <span>{torrent.category}</span>}{torrent.tags?.split(",").filter(Boolean).map((tag) => <span key={tag}>{tag.trim()}</span>)}</div></section>}
			<section className="torrent-files"><header><h3>内容文件</h3><span>{files ? `${files.file_count} 个 · ${formatBytes(files.total_size)}` : ""}</span></header>{loading ? <div className="dashboard-loading compact"><span /><strong>正在读取文件</strong></div> : files ? <div>{files.files.map((file) => <article key={`${file.index}-${file.name}`}><div><strong title={file.name}>{file.name}</strong><span>{formatBytes(file.size)}</span></div><b>{formatProgress(file.progress)}</b></article>)}{files.file_count > files.files.length && <p>仅显示前 {files.files.length} 个文件。</p>}</div> : <p>文件信息不可用。</p>}</section>
			<details className="torrent-technical"><summary>技术信息</summary><dl><div><dt>Info hash</dt><dd><code>{torrent.hash}</code></dd></div><div><dt>下载限速</dt><dd>{torrent.limits_available ? formatLimit(torrent.download_limit) : "列表未提供"}</dd></div><div><dt>上传限速</dt><dd>{torrent.limits_available ? formatLimit(torrent.upload_limit) : "列表未提供"}</dd></div><div><dt>添加时间</dt><dd>{formatUnix(torrent.added_on)}</dd></div></dl></details>
		</div>
	</aside></>;
}

function clampProgress(value: number) { return Math.max(0, Math.min(100, value * 100)); }
function formatProgress(value: number) { return `${clampProgress(value).toFixed(value >= .9995 ? 0 : 1)}%`; }
function shortHash(value: string) { return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value; }
function formatRatio(value: number) { return Number.isFinite(value) && value >= 0 ? value.toFixed(2) : "—"; }
function formatClock(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "刚刚" : date.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"}); }
function formatUnix(value: number) { return value > 0 ? new Date(value * 1000).toLocaleString("zh-CN") : "—"; }
function formatLimit(value: number) { return value > 0 ? formatRate(value) : "不限速"; }
function formatDuration(value: number) { if (!value || value < 1) return "—"; const days = Math.floor(value / 86400); const hours = Math.floor(value % 86400 / 3600); const minutes = Math.floor(value % 3600 / 60); return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`; }
function formatBytes(value: number) { if (!Number.isFinite(value) || value < 0) return "—"; const units = ["B", "KiB", "MiB", "GiB", "TiB"]; let size = value; let index = 0; while (size >= 1024 && index < units.length - 1) { size /= 1024; index++; } return `${size.toFixed(index === 0 ? 0 : size >= 100 ? 0 : size >= 10 ? 1 : 2)} ${units[index]}`; }
function formatRate(value: number) { return `${formatBytes(Math.max(0, value))}/s`; }
function networkLabel(value: Downloader["network_class"]) { return value === "seedbox" ? "盒子" : value === "home" ? "家宽" : "网络类型未标记"; }
