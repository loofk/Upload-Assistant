import {FormEvent, useCallback, useEffect, useState} from "react";
import type {ApiClient} from "./api";
import type {GlobalAuditEvent} from "./types";

export default function Audit({client, onError}: {client: ApiClient; onError: (reason: unknown) => void}) {
  const [events, setEvents] = useState<GlobalAuditEvent[]>([]);
  const [actorType, setActorType] = useState("");
  const [action, setAction] = useState("");
  const [resourceType, setResourceType] = useState("");
  const [resourceID, setResourceID] = useState("");
  const [nextCursor, setNextCursor] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (cursor = "", append = false) => {
    setLoading(true);
    try {
      const page = await client.listAuditEvents({actorType, action, resourceType, resourceID, cursor, limit: 50});
      setEvents((current) => append ? [...current, ...page.audit_events] : page.audit_events);
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (reason) {
      onError(reason);
    } finally {
      setLoading(false);
    }
  }, [action, actorType, client, onError, resourceID, resourceType]);

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const filter = (event: FormEvent) => {
    event.preventDefault();
    void load();
  };

  return <main className="audit-pane">
    <header className="audit-header content-toolbar">
      <p>配置变更与外部动作；任务步骤和证据仍在对应任务中查看。</p>
      <button className="secondary" onClick={() => void load()} disabled={loading}>刷新审计</button>
    </header>
    <form className="audit-filters" onSubmit={filter}>
      <label>资源类型<input value={resourceType} onChange={(event) => setResourceType(event.target.value)} placeholder="downloader" /></label>
      <label>资源 ID<input value={resourceID} onChange={(event) => setResourceID(event.target.value)} placeholder="box" /></label>
      <label>动作<input value={action} onChange={(event) => setAction(event.target.value)} placeholder="downloader.torrent.add" /></label>
      <label>执行者类型<input value={actorType} onChange={(event) => setActorType(event.target.value)} placeholder="worker" /></label>
      <button className="primary" type="submit" disabled={loading}>应用精确筛选</button>
    </form>
    <section className="audit-list" aria-busy={loading}>
      {events.map((event) => <details className="audit-card" key={event.id}>
        <summary>
          <span className="audit-action">{event.action}</span>
          <strong>{event.resource_type}{event.resource_id ? ` · ${event.resource_id}` : ""}</strong>
          <span>{event.actor_type}{event.actor_id ? ` · ${event.actor_id}` : ""}</span>
          <time>{formatDate(event.created_at)}</time>
        </summary>
        <div className="audit-detail">
          <div><span>事件 ID</span><code>{event.id}</code></div>
          {event.trace_id && <div><span>Trace ID</span><code>{event.trace_id}</code></div>}
          <pre className="json-block">{JSON.stringify(event.payload, null, 2)}</pre>
        </div>
      </details>)}
      {!loading && events.length === 0 && <div className="empty compact-empty">当前精确筛选条件下没有全局审计事件。</div>}
      {loading && events.length === 0 && <div className="skeleton-list"><i /><i /><i /></div>}
    </section>
    {hasMore && <button className="load-more audit-load-more" onClick={() => void load(nextCursor, true)} disabled={loading}>加载更早审计</button>}
  </main>;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {hour12: false});
}
