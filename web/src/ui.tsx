import {ReactNode, useEffect, useId, useRef} from "react";

export function InfoTip({label, children}: {label: string; children: ReactNode}) {
  const id = useId();
  return <span className="info-tip">
    <button type="button" className="info-trigger" aria-label={label} aria-describedby={id}>
      <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>
    </button>
    <span className="info-tooltip" id={id} role="tooltip">{children}</span>
  </span>;
}

export function SwitchField({checked, onChange, label, description, disabled = false}: {checked: boolean; onChange: (checked: boolean) => void; label: string; description?: string; disabled?: boolean}) {
  return <label className="switch-field">
    <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)}/>
    <span className="switch-control" aria-hidden="true"/>
    <span className="switch-copy"><strong>{label}</strong>{description && <small>{description}</small>}</span>
  </label>;
}

export function Drawer({open, title, description, dirty = false, onClose, children, footer}: {open: boolean; title: string; description?: string; dirty?: boolean; onClose: () => void; children: ReactNode; footer?: ReactNode}) {
  const titleID = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const dirtyRef = useRef(dirty);
  const onCloseRef = useRef(onClose);
  dirtyRef.current = dirty;
  onCloseRef.current = onClose;
  const requestClose = () => {
    if (dirtyRef.current && !window.confirm("有尚未保存的修改，确认关闭？")) return;
    onCloseRef.current();
  };
  useEffect(() => {
    if (!open) return;
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const timer = window.setTimeout(() => {
      const panel = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const firstField = panel?.querySelector<HTMLElement>('input:not([disabled]), select:not([disabled]), textarea:not([disabled])');
      (firstField ?? closeRef.current)?.focus();
    }, 0);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); requestClose(); }
      if (event.key !== "Tab") return;
      const panel = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const focusable = Array.from(panel?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])') ?? []);
      if (!focusable.length) return;
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.classList.add("drawer-open");
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("drawer-open");
      previousFocus.current?.focus();
    };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!open) return null;
  return <div className="drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose(); }}>
    <aside className="app-drawer" role="dialog" aria-modal="true" aria-labelledby={titleID}>
      <header><div><h2 id={titleID}>{title}</h2>{description && <p>{description}</p>}</div><button ref={closeRef} type="button" className="plain-close" aria-label="关闭" onClick={requestClose}>×</button></header>
      <div className="drawer-content">{children}</div>
      {footer && <footer className="drawer-footer">{footer}</footer>}
    </aside>
  </div>;
}

export function ResourceHeader({title, description, action}: {title: string; description?: string; action?: ReactNode}) {
  return <header className="resource-header"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{action}</header>;
}
