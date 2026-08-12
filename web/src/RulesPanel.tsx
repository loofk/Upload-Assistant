import {FormEvent, useCallback, useEffect, useMemo, useState} from "react";
import {ApiClient} from "./api";
import {Drawer, ResourceHeader, SwitchField} from "./ui";
import type {AdapterCatalogEnvelope, JsonValue, LLMProvider, RuleCollectionRun, RuleRevision, RuleReviewSection, RuleReviewWorkspace, RuleSourceInput, RuleSourceSet, SiteAccessPolicy, SiteAccessPolicyInput, SiteCredential, SiteSummary} from "./types";

type Props = {sites: SiteSummary[]; catalog: AdapterCatalogEnvelope | null; client: ApiClient; reloadSites: () => Promise<void>; onError: (reason: unknown) => void};
type ReviewTab = "hard-gates" | "advisories" | "original";
type WorkbenchStage = "overview" | "sources" | "review" | "runtime";
const APPROVAL_AUDIT_COMMENT = "已在规则页确认全部硬门禁";

export default function RulesPanel({sites, catalog, client, reloadSites, onError}: Props) {
  const [siteCode, setSiteCode] = useState("");
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState("");
  const [revisions, setRevisions] = useState<RuleRevision[]>([]);
  const [selectedRevisionID, setSelectedRevisionID] = useState("");
  const [review, setReview] = useState<RuleReviewWorkspace | null>(null);
  const [original, setOriginal] = useState("");
  const [credentials, setCredentials] = useState<SiteCredential[]>([]);
  const [accessPolicy, setAccessPolicy] = useState<SiteAccessPolicy | null>(null);
  const [sourceSet, setSourceSet] = useState<RuleSourceSet | null>(null);
  const [ruleSources, setRuleSources] = useState<RuleSourceInput[]>([newRuleSource(0)]);
  const [scopeConfirmed, setScopeConfirmed] = useState(false);
  const [cookieHostsConfirmed, setCookieHostsConfirmed] = useState(false);
  const [collectionRun, setCollectionRun] = useState<RuleCollectionRun | null>(null);
  const [collectionError, setCollectionError] = useState<{code: string; message: string} | null>(null);
  const [tab, setTab] = useState<ReviewTab>("hard-gates");
  const [stage, setStage] = useState<WorkbenchStage>("overview");
  const [busy, setBusy] = useState(false);
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [providerID, setProviderID] = useState("");
  const [siteEditor, setSiteEditor] = useState<"new" | "edit" | "">("");
  const [siteEditorDirty, setSiteEditorDirty] = useState(false);
  const [showUnconfigured, setShowUnconfigured] = useState(false);

  const runtimeSites = useMemo(() => new Set((catalog?.adapters ?? []).filter((item) => item.kind === "site" && item.runtime_supported && item.site_code).map((item) => item.site_code as string)), [catalog]);
  const orderedSites = useMemo(() => [...sites].sort((left, right) => {
    const rank = (site: SiteSummary) => [site.enabled ? 1 : 0, runtimeSites.has(site.code) ? 1 : 0, site.active_rule_fingerprint ? 1 : 0, site.rule_revision_count > 0 ? 1 : 0];
    const leftRank = rank(left); const rightRank = rank(right);
    for (let index = 0; index < leftRank.length; index++) if (leftRank[index] !== rightRank[index]) return rightRank[index] - leftRank[index];
    return left.code.localeCompare(right.code);
  }), [runtimeSites, sites]);
  const unconfiguredCount = sites.filter((site) => !site.rule_revision_count).length;
  const tags = useMemo(() => [...new Set(sites.flatMap((site) => site.tags ?? []))].sort(), [sites]);
  const filteredSites = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return orderedSites.filter((site) => (showUnconfigured || site.rule_revision_count > 0) && (!tag || site.tags?.includes(tag)) && (!needle || [site.code, site.name, ...(site.aliases ?? []), ...(site.tags ?? [])].some((value) => value.toLocaleLowerCase().includes(needle))));
  }, [orderedSites, query, showUnconfigured, tag]);

  useEffect(() => {
    const current = sites.find((site) => site.code === siteCode);
    if (!siteCode || !current || (!showUnconfigured && current.rule_revision_count === 0)) setSiteCode(orderedSites.find((site) => showUnconfigured || site.rule_revision_count > 0)?.code ?? "");
  }, [orderedSites, showUnconfigured, siteCode, sites]);
  useEffect(() => {
    void client.listLLMProviders().then((items) => {
      const available = items.filter((item) => item.enabled && item.use_cases.includes("rule_analysis"));
      setProviders(available);
      setProviderID((current) => available.some((item) => item.id === current) ? current : available[0]?.id ?? "");
    }).catch(onError);
  }, [client, onError]);
  useEffect(() => {
    setRevisions([]); setSelectedRevisionID(""); setReview(null); setOriginal(""); setSourceSet(null); setCollectionRun(null);
    setRuleSources([newRuleSource(0)]); setScopeConfirmed(false); setCookieHostsConfirmed(false); setCollectionError(null);
    setStage("overview");
  }, [siteCode]);

  const loadSite = useCallback(async () => {
    if (!siteCode) return;
    try {
      const [nextRevisions, nextCredentials, nextAccessPolicy, nextSourceSet, nextCollectionRun] = await Promise.all([
        client.listRuleRevisions(siteCode), client.listSiteCredentials(siteCode), client.getSiteAccessPolicy(siteCode),
        client.getRuleSourceSet(siteCode), client.latestRuleCollectionRun(siteCode),
      ]);
      setRevisions(nextRevisions);
      setCredentials(nextCredentials);
      setAccessPolicy(nextAccessPolicy);
      setSourceSet(nextSourceSet);
      setRuleSources(nextSourceSet.sources.length ? nextSourceSet.sources : [newRuleSource(0)]);
      setScopeConfirmed(nextSourceSet.scope_confirmed);
      setCookieHostsConfirmed(nextSourceSet.cookie_hosts_confirmed);
      setCollectionRun(nextCollectionRun);
      const site = sites.find((item) => item.code === siteCode);
      const baseline = nextRevisions.find((item) => item.id === site?.active_rule_revision_id || item.fingerprint === site?.active_rule_fingerprint);
      const pending = nextRevisions.find((item) => item.id !== baseline?.id && (item.status === "draft" || item.status === "approved"));
      setSelectedRevisionID((current) => nextRevisions.some((item) => item.id === current) ? current : pending?.id ?? baseline?.id ?? nextRevisions[0]?.id ?? "");
    } catch (reason) { onError(reason); }
  }, [client, onError, siteCode, sites]);
  useEffect(() => { void loadSite(); }, [loadSite]);

  useEffect(() => {
    if (!selectedRevisionID) { setReview(null); setOriginal(""); return; }
    let active = true;
    void Promise.all([client.getRuleReview(selectedRevisionID), client.getRuleMarkdown(selectedRevisionID)])
      .then(([nextReview, nextOriginal]) => { if (active) { setReview(nextReview); setOriginal(nextOriginal); } })
      .catch(onError);
    return () => { active = false; };
  }, [client, onError, selectedRevisionID]);

  const mutate = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try { await action(); await Promise.all([loadSite(), reloadSites()]); }
    catch (reason) { onError(reason); }
    finally { setBusy(false); }
  };

  const selectedSite = sites.find((site) => site.code === siteCode);
  const baselineRevision = revisions.find((item) => item.id === selectedSite?.active_rule_revision_id || item.fingerprint === selectedSite?.active_rule_fingerprint);
  const pendingRevision = revisions.find((item) => item.id !== baselineRevision?.id && (item.status === "draft" || item.status === "approved"));
  const historyRevisions = revisions.filter((item) => item.id !== baselineRevision?.id && item.id !== pendingRevision?.id);
  const selectedRevision = revisions.find((item) => item.id === selectedRevisionID);
  const selectedIsBaseline = Boolean(selectedRevision && baselineRevision?.id === selectedRevision.id);
  const selectedObligations = (selectedRevision?.obligations ?? []).filter(isRecordValue);
  const reviewSections = Array.isArray(review?.sections) ? review.sections : [];
  const reviewAdvisories = Array.isArray(review?.advisories) ? review.advisories : [];
  const reviewBlockers = Array.isArray(review?.blockers) ? review.blockers : [];
  const sourceConflicts = reviewBlockers.filter((item) => item.code === "rule_source_conflict");
  const cookieConfigured = sourceSet?.cookie_configured ?? credentials.some((item) => item.name === "cookie" && item.enabled);
  const refreshReview = async (workspace?: RuleReviewWorkspace) => {
    if (workspace) setReview(workspace);
    else if (selectedRevisionID) setReview(await client.getRuleReview(selectedRevisionID));
  };
  const correctHardGate = async (revision: RuleRevision, section: string, data: Record<string, JsonValue>, comment: string) => {
    setBusy(true);
    try {
      const corrected = await client.correctRuleHardGate(revision, section, data, comment);
      await Promise.all([loadSite(), reloadSites()]);
      setSelectedRevisionID(corrected.id);
      setTab("hard-gates"); setStage("review");
    } catch (reason) { onError(reason); }
    finally { setBusy(false); }
  };
	const updateRuleSource = (index: number, patch: Partial<RuleSourceInput>) => {
		setRuleSources((current) => current.map((item, itemIndex) => itemIndex === index ? {...item, ...patch} : item));
		if (patch.url !== undefined || patch.auth_mode !== undefined) setCookieHostsConfirmed(false);
	};
	const saveAndCollect = async () => {
		if (!selectedSite) return;
		setBusy(true); setCollectionError(null);
		let createdRunID = "";
		try {
			const currentRun = collectionRun && collectionRun.status !== "ready" && collectionRun.status !== "failed" ? collectionRun : null;
			if (currentRun) {
				let latest = currentRun;
				const controller = new AbortController();
				await client.streamRuleCollectionRun(currentRun.id, (next) => { latest = next; setCollectionRun(next); }, controller.signal);
				if (latest.status === "ready" && latest.rule_revision_id) {
					await Promise.all([loadSite(), reloadSites()]);
					setSelectedRevisionID(latest.rule_revision_id); setTab("hard-gates"); setStage("review");
				} else if (latest.status === "failed") setCollectionError({code: latest.error_code ?? "rule_collection_failed", message: latest.error_detail ?? "规则采集失败"});
				return;
			}
			const saved = await client.putRuleSourceSet(selectedSite.code, {sources: normalizeRuleSources(ruleSources), scope_confirmed: scopeConfirmed, cookie_hosts_confirmed: cookieHostsConfirmed});
			setSourceSet(saved);
			setRuleSources(saved.sources);
			const created = await client.createRuleCollectionRun(selectedSite.code, saved.fingerprint, providerID);
			createdRunID = created.id;
			setCollectionRun(created);
			let latest = created;
			const controller = new AbortController();
			await client.streamRuleCollectionRun(created.id, (next) => { latest = next; setCollectionRun(next); }, controller.signal);
			if (latest.status === "ready" && latest.rule_revision_id) {
				await Promise.all([loadSite(), reloadSites()]);
				setSelectedRevisionID(latest.rule_revision_id); setTab("hard-gates"); setStage("review");
			} else if (latest.status === "failed") setCollectionError({code: latest.error_code ?? "rule_collection_failed", message: latest.error_detail ?? "规则采集失败"});
		} catch (reason) {
			setCollectionError(toActionError(reason, "rule_collection_failed"));
			if (createdRunID) client.getRuleCollectionRun(createdRunID).then((latest) => {
				setCollectionRun(latest);
				if (latest.status === "ready" && latest.rule_revision_id) {
					void Promise.all([loadSite(), reloadSites()]).then(() => { setSelectedRevisionID(latest.rule_revision_id ?? ""); setTab("hard-gates"); setStage("review"); });
				}
			}).catch(() => undefined);
		} finally { setBusy(false); }
	};
	const saveRuleSources = async () => {
		if (!selectedSite) return;
		setBusy(true); setCollectionError(null);
		try {
			const saved = await client.putRuleSourceSet(selectedSite.code, {sources: normalizeRuleSources(ruleSources), scope_confirmed: scopeConfirmed, cookie_hosts_confirmed: cookieHostsConfirmed});
			setSourceSet(saved); setRuleSources(saved.sources);
		}
		catch (reason) { setCollectionError(toActionError(reason, "rule_sources_save_failed")); }
		finally { setBusy(false); }
	};
	const refreshCollection = async () => {
		if (!selectedSite) return;
		setBusy(true); setCollectionError(null);
		try { setCollectionRun(await client.latestRuleCollectionRun(selectedSite.code)); }
		catch (reason) { setCollectionError(toActionError(reason, "rule_collection_read_failed")); }
		finally { setBusy(false); }
	};
	const cookieRequired = ruleSources.some((item) => (item.auth_mode ?? "site_cookie") === "site_cookie");
	const collectionReady = Boolean(providerID && (!cookieRequired || cookieConfigured) && accessPolicy?.operator_policy?.enabled && ruleSources.length && ruleSources.every((item) => item.url.trim()) && scopeConfirmed && (!cookieRequired || cookieHostsConfirmed));
  const openSiteEditor = (mode: "new" | "edit") => { setSiteEditorDirty(false); setSiteEditor(mode); };

  return <section className="rules-workspace">
	    <div className="rules-toolbar"><ResourceHeader title="站点规则编译与硬门禁" description="采集规则后，只需审核上传限速、下载限速和分类命名。" action={<div className="rules-site-actions"><button className="secondary" onClick={() => openSiteEditor("new")}>新增站点</button><button className="secondary" disabled={!selectedSite} onClick={() => openSiteEditor("edit")}>编辑站点</button></div>}/></div>

    <Drawer open={Boolean(siteEditor)} dirty={siteEditorDirty} onClose={() => setSiteEditor("")} title={siteEditor === "new" ? "新增站点" : `编辑站点 · ${selectedSite?.name ?? ""}`} description="已有站点的系统代码和适配器不可修改。">
      <SiteEditor site={siteEditor === "edit" ? selectedSite : undefined} client={client} knownTags={tags} onDirty={() => setSiteEditorDirty(true)} onSaved={async (code) => { setShowUnconfigured(true); await reloadSites(); setSiteCode(code); setSiteEditorDirty(false); setSiteEditor(""); }} onError={onError}/>
    </Drawer>

    <aside className="site-browser" aria-label="站点列表">
      <div className="site-directory-tools">
        <input aria-label="搜索站点" placeholder="搜索站点" value={query} onChange={(event) => setQuery(event.target.value)} />
        <select aria-label="按标签筛选" value={tag} onChange={(event) => setTag(event.target.value)}><option value="">全部标签</option>{tags.map((item) => <option key={item}>{item}</option>)}</select>
        <label className="show-unconfigured"><input type="checkbox" checked={showUnconfigured} onChange={(event) => setShowUnconfigured(event.target.checked)} />显示未配置站点 <small>{unconfiguredCount}</small></label>
      </div>
      <div className="site-directory">{filteredSites.map((site) => <button key={site.code} className={site.code === siteCode ? "active" : ""} onClick={() => setSiteCode(site.code)} title={`系统标识 ${site.code}`}>
        <span className="site-title"><strong>{site.name}</strong><i className={site.enabled && runtimeSites.has(site.code) ? "available" : ""}>{!site.enabled ? "已停用" : runtimeSites.has(site.code) ? "运行时可用" : site.adapter === "config_only" ? "仅配置" : "未接入运行时"}</i></span>
        <span className="chip-row">{(site.tags ?? []).map((item) => <small key={`t-${item}`} className="tag">{item}</small>)}</span>
        <em>{site.active_rule_fingerprint ? "规则已生效" : site.rule_revision_count > 0 ? `${site.rule_revision_count} 个规则版本，尚未激活` : "未配置规则"}</em>
      </button>)}{!filteredSites.length && <Empty text={showUnconfigured ? "当前搜索条件下没有站点。" : "还没有已配置规则的站点；可打开“显示未配置站点”后选择并导入。"} />}</div>
    </aside>

    {selectedSite && <main className="rule-workbench">
      <nav className="rules-stage-nav" aria-label="站点规则阶段">{(["overview", "sources", "review", "runtime"] as WorkbenchStage[]).map((value) => <button key={value} className={stage === value ? "active" : ""} aria-current={stage === value ? "step" : undefined} onClick={() => setStage(value)}><span>{value === "overview" ? "1" : value === "sources" ? "2" : value === "review" ? "3" : "4"}</span>{stageLabel(value)}</button>)}</nav>

      {stage === "overview" && <section className="rule-stage-panel rule-overview"><ResourceHeader title={selectedSite.name} description="当前规则状态和下一步操作。"/><div className="rule-overview-grid"><article><span>当前基准</span><strong>{baselineRevision ? `r${baselineRevision.revision}` : "尚未建立"}</strong><small>{selectedSite.active_rule_fingerprint ? "已应用到新任务" : "尚未激活规则"}</small></article><article><span>待处理变更</span><strong>{pendingRevision ? `r${pendingRevision.revision}` : "无"}</strong><small>{pendingRevision ? revisionStatusLabel(pendingRevision, selectedSite) : "当前没有待审核变更"}</small></article><article><span>规则来源</span><strong>{sourceSet?.sources.length ?? 0} 个页面</strong><small>{sourceSet?.scope_confirmed ? "来源完整" : "完整性未确认"}</small></article><article><span>硬门禁</span><strong>{review ? `${review.confirmed_count} / ${review.required_count}` : "尚未分析"}</strong><small>上传、下载、分类命名</small></article></div><div className="rule-next-action"><div><strong>{pendingRevision ? "下一步：审核本次变更" : baselineRevision ? "规则基准已就绪" : "下一步：配置并采集规则来源"}</strong><span>{pendingRevision ? "确认三项硬门禁后批准并应用。" : baselineRevision ? "需要更新时重新采集规则页面。" : "添加规则链接并确认来源完整性。"}</span></div><button className="primary" onClick={() => setStage(pendingRevision ? "review" : baselineRevision ? "runtime" : "sources")}>{pendingRevision ? "开始审核" : baselineRevision ? "查看运行配置" : "配置规则来源"}</button></div></section>}

      {stage === "sources" && <section className="rule-stage-panel"><RuleSourceCompiler site={selectedSite} providers={providers} providerID={providerID} onProviderID={setProviderID}
          sources={ruleSources} onSource={updateRuleSource} onAddSource={() => setRuleSources((current) => [...current, newRuleSource(current.length)])}
          onRemoveSource={(index) => { setRuleSources((current) => current.filter((_, itemIndex) => itemIndex !== index)); setCookieHostsConfirmed(false); }}
          scopeConfirmed={scopeConfirmed} onScopeConfirmed={setScopeConfirmed} cookieHostsConfirmed={cookieHostsConfirmed} onCookieHostsConfirmed={setCookieHostsConfirmed}
          cookieConfigured={cookieConfigured} accessReady={Boolean(accessPolicy?.operator_policy?.enabled)} sourceSet={sourceSet} run={collectionRun}
          error={collectionError} busy={busy} canCollect={collectionReady} onSave={() => void saveRuleSources()} onCollect={() => void saveAndCollect()} onRefresh={() => void refreshCollection()}
          onCredentials={() => setStage("runtime")} /></section>}

      {stage === "review" && <section className="rule-stage-panel rule-review-main">
	        <div className="revision-header"><div><h3>{selectedSite.name}</h3><span>一份生效基准和至多一份待应用变更。</span></div><div className="baseline-switch">
	          {baselineRevision && <button className={baselineRevision.id === selectedRevisionID ? "active" : ""} onClick={() => setSelectedRevisionID(baselineRevision.id)}><small>当前基准</small><strong>r{baselineRevision.revision}</strong></button>}
	          {pendingRevision && <button className={pendingRevision.id === selectedRevisionID ? "active pending" : "pending"} onClick={() => setSelectedRevisionID(pendingRevision.id)}><small>{pendingRevision.status === "approved" ? "已审核，待应用" : "待审核变更"}</small><strong>r{pendingRevision.revision}</strong></button>}
	        </div></div>
	        {(baselineRevision || pendingRevision) && <RuleBaselineDiff baseline={baselineRevision} pending={pendingRevision} />}
	        {historyRevisions.length > 0 && <details className="revision-history"><summary>历史记录（{historyRevisions.length}）</summary><div>{historyRevisions.map((revision) => <button key={revision.id} className={revision.id === selectedRevisionID ? "active" : ""} onClick={() => setSelectedRevisionID(revision.id)}>r{revision.revision}<span>{revisionStatusLabel(revision, selectedSite)}</span><time>{new Date(revision.created_at).toLocaleDateString()}</time></button>)}</div><p>历史原文、指纹与审批证据仅供审计，不会参与运行时。</p></details>}
	        {!revisions.length && <Empty text="该站点尚未建立规则基准。请先在“规则来源”中采集并生成变更。" />}
        {selectedRevision && review && <>
	          <section className="review-progress"><div><strong>{review.confirmed_count} / {review.required_count}</strong><span>已确认硬门禁</span></div><div className="review-progress-track"><i style={{width: `${review.required_count ? review.confirmed_count / review.required_count * 100 : 0}%`}} /></div><code title={`规则指纹 ${review.fingerprint}`}>{shortHash(review.fingerprint)}</code></section>
	          <nav className="review-tabs"><button className={tab === "hard-gates" ? "active" : ""} onClick={() => setTab("hard-gates")}>硬门禁审核</button><button className={tab === "advisories" ? "active" : ""} onClick={() => setTab("advisories")}>转种前提示 {reviewAdvisories.length + selectedObligations.length}</button><button className={tab === "original" ? "active" : ""} onClick={() => setTab("original")}>原始规则全文</button></nav>
	          {tab === "hard-gates" && <><div className="review-source-conflicts">{sourceConflicts.map((conflict, index) => <article key={`${conflict.section}-${index}`}><div><strong>多来源冲突 · {conflict.section ?? "规则"}</strong><span>{conflict.message}</span></div>{conflict.evidence_refs?.length ? <p>{conflict.evidence_refs.map((reference) => <code key={reference}>{reference}</code>)}</p> : null}<small>对照证据后，在对应硬门禁使用“纠正规则配置”派生新 revision；原 revision 不会被覆盖。</small></article>)}</div><div className="review-section-list">{reviewSections.map((section) => <ReviewSectionCard key={section.key} section={section} revision={selectedRevision} client={client} busy={busy} onBusy={setBusy} onSaved={refreshReview} onCorrected={correctHardGate} onError={onError} />)}</div></>}
	          {tab === "advisories" && <section className="obligation-list">{reviewAdvisories.map((item, index) => <AdvisoryCard key={`${item.section}-${index}`} item={item} />)}{selectedObligations.map((item, index) => <ObligationCard key={String(item.id ?? index)} item={item} />)}{!reviewAdvisories.length && !selectedObligations.length && <Empty text="这个规则版本没有转种前提示。" />}</section>}
          {tab === "original" && <section className="original-rule"><header><div><strong>不可变 Markdown 原文</strong><span>sha256: {selectedRevision.markdown_sha256}</span></div><a href={selectedRevision.source_url} target="_blank" rel="noreferrer">打开规则来源 ↗</a></header><pre>{original}</pre></section>}
		          <footer className="rule-approval-bar"><div>{selectedIsBaseline ? <><strong className="ready">当前生效基准</strong><span>新任务会绑定此规则指纹。</span></> : selectedRevision.status === "approved" ? <><strong className="ready">变更已通过审核，等待应用</strong><span>应用后它会替换当前基准，旧基准进入历史。</span></> : selectedRevision.status === "retired" ? <><strong>历史记录（只读）</strong><span>它不会参与运行时，也不能直接覆盖当前基准。</span></> : review.approval_ready ? <><strong className="ready">三项硬门禁已确认</strong><span>批准后仍需明确应用，才会替换当前基准。</span></> : <><strong>变更仍需审核</strong><span>{reviewBlockers.slice(0, 3).map((item) => item.section ? `${item.section}: ${item.message}` : item.message).join("；")}</span></>}</div>
	            {selectedRevision.status === "draft" && <div className="rule-approval-actions"><button className="danger" disabled={busy} onClick={() => { if (window.confirm(`放弃 r${selectedRevision.revision} 这次变更？它将移入审计历史，不会删除证据。`)) void mutate(async () => { await client.discardRuleDraft(selectedRevision); setSelectedRevisionID(baselineRevision?.id ?? ""); }); }}>放弃变更</button><button className="primary" disabled={busy || !review.approval_ready} onClick={() => { if (window.confirm(`批准 ${selectedRevision.site_code} r${selectedRevision.revision}？审批将绑定当前规则指纹。`)) void mutate(() => client.approveRule(selectedRevision, APPROVAL_AUDIT_COMMENT)); }}>批准变更</button></div>}
	            {selectedRevision.status === "approved" && !selectedIsBaseline && <button className="primary" disabled={busy} onClick={() => { if (window.confirm("应用此变更为当前基准？后续新任务将绑定新 fingerprint。")) void mutate(() => client.activateRule(selectedRevision.id)); }}>应用为当前基准</button>}
	          </footer>
        </>}
      </section>}

	      {stage === "runtime" && <section className="rule-stage-panel rule-runtime-stage"><ResourceHeader title="运行配置" description="访问频率和凭据由规则采集、搜索与转种流程共用。"/>
        {accessPolicy && <AccessPolicyEditor key={siteCode} policy={accessPolicy} client={client} onSaved={setAccessPolicy} onError={onError} />}
        <SiteCredentialsEditor key={`credentials-${siteCode}`} site={selectedSite} catalog={catalog} sources={ruleSources} credentials={credentials} client={client} onSaved={setCredentials} onError={onError} />
      </section>}
    </main>}
  </section>;
}

function RuleBaselineDiff({baseline, pending}: {baseline?: RuleRevision; pending?: RuleRevision}) {
  const gates = [
    {key: "upload", label: "上传限速"},
    {key: "download", label: "下载限速"},
    {key: "naming", label: "分类命名"},
  ];
  return <section className={`baseline-diff ${pending ? "has-change" : "stable"}`} aria-label="规则基准差异">
    <div className="baseline-flow"><span><small>当前基准</small><strong>{baseline ? `r${baseline.revision}` : "尚未建立"}</strong></span><i aria-hidden="true">→</i><span><small>待应用变更</small><strong>{pending ? `r${pending.revision}` : "无"}</strong></span></div>
    <div className="gate-diff-list">{gates.map((gate) => {
      const changed = Boolean(pending && (!baseline || ruleGateSnapshot(baseline, gate.key) !== ruleGateSnapshot(pending, gate.key)));
      return <span className={changed ? "changed" : "unchanged"} key={gate.key}>{gate.label}<small>{pending ? changed ? "有变化" : "未变化" : "当前值"}</small></span>;
    })}</div>
    <p>{pending ? "审核的是这次变化；应用后，它会成为唯一的运行时基准。" : "当前没有待审核或待应用的规则变更。"}</p>
  </section>;
}

function RuleSourceCompiler({site, providers, providerID, onProviderID, sources, onSource, onAddSource, onRemoveSource, scopeConfirmed, onScopeConfirmed, cookieHostsConfirmed, onCookieHostsConfirmed, cookieConfigured, accessReady, sourceSet, run, error, busy, canCollect, onSave, onCollect, onRefresh, onCredentials}: {
  site: SiteSummary; providers: LLMProvider[]; providerID: string; onProviderID: (value: string) => void;
  sources: RuleSourceInput[]; onSource: (index: number, patch: Partial<RuleSourceInput>) => void; onAddSource: () => void; onRemoveSource: (index: number) => void;
  scopeConfirmed: boolean; onScopeConfirmed: (value: boolean) => void; cookieHostsConfirmed: boolean; onCookieHostsConfirmed: (value: boolean) => void;
  cookieConfigured: boolean; accessReady: boolean; sourceSet: RuleSourceSet | null; run: RuleCollectionRun | null; error: {code: string; message: string} | null;
  busy: boolean; canCollect: boolean; onSave: () => void; onCollect: () => void; onRefresh: () => void; onCredentials: () => void;
}) {
  const cookieSources = sources.filter((item) => (item.auth_mode ?? "site_cookie") === "site_cookie");
  const cookieRequired = cookieSources.length > 0;
  const hosts = [...new Set(cookieSources.map((item) => ruleSourceHost(item.url)).filter(Boolean))];
  const status = run?.status ?? "idle";
  const selectedProvider = providers.find((item) => item.id === providerID);
  const failureCode = error?.code ?? run?.error_code ?? "";
  const allEvidenceReady = Boolean(run?.documents.length && run.documents.every((item) => item.status === "ready"));
  return <section className="rule-compiler-console">
    <header><div><span className="eyebrow">规则来源</span><h3>添加站点规则页面</h3><p>粘贴一个或多个 HTTPS 地址。系统按访问频率逐页读取，再生成一份待审核变更。</p></div><button className="secondary compact" disabled={busy} onClick={onRefresh}>刷新状态</button></header>
    <div className="compiler-body">
      <div className="rule-source-list">
        <div className="compiler-section-title"><div><strong>规则页面</strong><small>最多 20 个；页面说明不填时会自动生成，证据编号由系统维护。</small></div><button className="secondary compact" disabled={busy || sources.length >= 20} onClick={onAddSource}>添加页面</button></div>
        {sources.map((source, index) => <article className="rule-source-row" key={`${source.id}-${index}`}>
          <span className="source-index">{String(index + 1).padStart(2, "0")}</span>
          <label className="source-url">规则页面地址<input type="url" value={source.url} onChange={(event) => onSource(index, {url: event.target.value})} placeholder="https://站点/规则页面" /></label>
          <label className="source-auth">访问方式<select value={source.auth_mode ?? "site_cookie"} onChange={(event) => onSource(index, {auth_mode: event.target.value as "none" | "site_cookie"})}><option value="none">无需认证</option><option value="site_cookie">站点 Cookie</option></select></label>
          <label className="source-scope">页面说明（可不填）<input value={source.scope} onChange={(event) => onSource(index, {scope: event.target.value})} placeholder="例如：上传标题规则" /></label>
          <button className="icon-button" aria-label={`删除来源 ${index + 1}`} disabled={busy || sources.length === 1} onClick={() => onRemoveSource(index)}>×</button>
        </article>)}
        <div className={`source-confirmations ${cookieRequired ? "" : "public-only"}`}>
          <fieldset className="source-completeness">
            <legend>规则来源是否完整</legend>
            <div className="binary-choice">
              <label><input type="radio" name={`rule-source-complete-${site.code}`} checked={scopeConfirmed} onChange={() => onScopeConfirmed(true)} /><span>是</span></label>
              <label><input type="radio" name={`rule-source-complete-${site.code}`} checked={!scopeConfirmed} onChange={() => onScopeConfirmed(false)} /><span>否</span></label>
            </div>
            <small>选择“否”时可以保存链接，但不能采集并生成可应用的变更。</small>
          </fieldset>
          {cookieRequired && <label><input type="checkbox" checked={cookieHostsConfirmed} onChange={(event) => onCookieHostsConfirmed(event.target.checked)} /><span><strong>仅允许向这些站点发送 Cookie</strong><small>{hosts.length ? hosts.join("、") : "请先填写有效 HTTPS 地址"}</small></span></label>}
        </div>
      </div>
      <aside className="compiler-settings" aria-label="采集设置">
        <div className="compiler-setting-card"><label>分析模型<select value={providerID} onChange={(event) => onProviderID(event.target.value)}>{providers.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}</select></label>
        {selectedProvider && <p className="provider-runtime-summary">{selectedProvider.reasoning_effort === "default" ? "默认推理" : `${selectedProvider.reasoning_effort} 推理`} · {selectedProvider.timeout_seconds} 秒 · {selectedProvider.streaming_enabled ? "SSE 流式" : "整包响应"}</p>}
        {selectedProvider && selectedProvider.timeout_seconds < 600 && ["high", "xhigh", "max"].includes(selectedProvider.reasoning_effort) && <p className="compiler-timeout-warning">多页规则配合高推理可能超过当前超时；<a href="/app/configuration/ai-models">将模型超时调到 600 秒</a>。</p>}
        {!providers.length && <p className="compiler-blocker">没有启用“站点规则分析”的模型。</p>}
        </div>
        <div className="compiler-setting-card"><div className={`compiler-readiness ${!cookieRequired || cookieConfigured ? "ready" : "blocked"}`}><div><strong>{cookieRequired ? cookieConfigured ? "Cookie 已配置" : "需要配置 Cookie" : "规则页面无需登录"}</strong><ContextHelp id={`rule-source-credential-help-${site.code}`} label="规则页面凭据说明" text={cookieRequired ? "Cookie 只会发送到上方明确选择“站点 Cookie”的 HTTPS 地址；值不会回显或写入日志。" : "公开规则页面不会收到 Cookie、Passkey 或 API Key。"} /></div>{cookieRequired && !cookieConfigured && <button type="button" className="secondary compact" onClick={onCredentials}>配置凭据</button>}</div></div>
        <div className="compiler-actions"><button className="secondary" disabled={busy || !sources.every((item) => item.url.trim())} onClick={onSave}>保存链接</button><button className="primary" disabled={busy || !canCollect} onClick={onCollect}>{busy && status !== "idle" ? "正在采集/分析…" : status === "queued" || status === "fetching" || status === "analyzing" ? "继续等待当前采集" : "采集并生成变更"}</button></div>
      </aside>
    </div>
    {run && <section className={`collection-evidence ${run.status}`}><header><div><strong>{collectionStatusLabel(run.status)}</strong><span>run {shortHash(run.id)} · {run.documents.filter((item) => item.status === "ready").length}/{run.documents.length} 页已取证</span></div>{run.rule_revision_id && <code>revision {shortHash(run.rule_revision_id)}</code>}</header><div>{run.documents.map((document) => <article key={document.id}><i className={document.status} /><span><strong>{document.scope}</strong><small>{document.source_id} · {document.auth_mode === "none" ? "无需认证" : "Cookie"} · {document.status}{document.http_status ? ` · HTTP ${document.http_status}` : ""}{document.size_bytes ? ` · ${formatBytes(document.size_bytes)}` : ""}</small></span>{document.text_sha256 && <code>{shortHash(document.text_sha256)}</code>}</article>)}</div></section>}
    {(error || run?.status === "failed") && <div className="rule-analysis-error" role="alert"><strong>{failureCode || "rule_collection_failed"}</strong><span>{error?.message ?? run?.error_detail ?? "规则采集失败"}</span><small>{ruleCollectionRecovery(failureCode, allEvidenceReady, selectedProvider?.timeout_seconds)}</small></div>}
  </section>;
}

function AccessPolicyEditor({policy, client, onSaved, onError}: {policy: SiteAccessPolicy; client: ApiClient; onSaved: (policy: SiteAccessPolicy) => void; onError: (reason: unknown) => void}) {
  const defaults: SiteAccessPolicyInput = {enabled: false, general_min_interval_seconds: 10, general_max_requests_per_hour: 120, search_min_interval_seconds: 30, search_max_requests_per_hour: 30, max_concurrency: 1};
  const [form, setForm] = useState<SiteAccessPolicyInput>(policy.operator_policy ?? defaults);
  const [adjusting, setAdjusting] = useState(false);
  const [busy, setBusy] = useState(false);
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { const next = await client.putSiteAccessPolicy(policy.site_code, form); onSaved(next); }
    catch (reason) { onError(reason); }
    finally { setBusy(false); }
  };
  const setNumber = (key: keyof SiteAccessPolicyInput, value: string) => setForm({...form, [key]: Number(value)});
  const savedEnabled = policy.operator_policy?.enabled ?? false;
  return <section className="rule-side-card access-policy-editor" aria-labelledby={`access-policy-title-${policy.site_code}`}>
    <header className="rule-side-card-header"><div><h3 id={`access-policy-title-${policy.site_code}`}>站点访问频率</h3><ContextHelp id={`access-policy-help-${policy.site_code}`} label="站点访问频率说明" text="这是当前站点唯一的访问频率配置。规则采集、站点搜索和转种流程共用同一套请求间隔、小时配额与并发计数；已生效规则只能进一步收紧或阻止访问。" /></div><span className={`side-card-state ${savedEnabled ? "enabled" : "disabled"}`}>{savedEnabled ? "已启用" : "未启用"}</span></header>
    <form className="access-policy-form" onSubmit={save}><SwitchField checked={form.enabled} onChange={(enabled) => setForm({...form, enabled})} label="启用站点访问" description="规则采集、搜索和转种流程共用这套限制。"/>
      <div className="access-policy-summary"><article><span>普通请求</span><strong>{form.general_min_interval_seconds} 秒间隔</strong><small>{form.general_max_requests_per_hour} 次 / 小时</small></article><article><span>搜索请求</span><strong>{form.search_min_interval_seconds} 秒间隔</strong><small>{form.search_max_requests_per_hour} 次 / 小时</small></article><article><span>同时请求</span><strong>{policy.active_requests} / {form.max_concurrency}</strong><small>当前请求 / 并发上限</small></article></div>
      {adjusting && <div className="access-policy-grid"><label>普通请求最小间隔（秒）<input aria-label="普通请求最小间隔（秒）" type="number" min="1" max="86400" required disabled={!form.enabled} value={form.general_min_interval_seconds} onChange={(event) => setNumber("general_min_interval_seconds", event.target.value)} /></label><label>普通请求每小时上限<input aria-label="普通请求每小时上限" type="number" min="1" max="3600" required disabled={!form.enabled} value={form.general_max_requests_per_hour} onChange={(event) => setNumber("general_max_requests_per_hour", event.target.value)} /></label><label>搜索最小间隔（秒）<input aria-label="搜索最小间隔（秒）" type="number" min="1" max="86400" required disabled={!form.enabled} value={form.search_min_interval_seconds} onChange={(event) => setNumber("search_min_interval_seconds", event.target.value)} /></label><label>搜索每小时上限<input aria-label="搜索每小时上限" type="number" min="1" max="3600" required disabled={!form.enabled} value={form.search_max_requests_per_hour} onChange={(event) => setNumber("search_max_requests_per_hour", event.target.value)} /></label><label>站点并发上限<input aria-label="站点并发上限" type="number" min="1" max="4" required disabled={!form.enabled} value={form.max_concurrency} onChange={(event) => setNumber("max_concurrency", event.target.value)} /></label></div>}
      <footer className="access-policy-actions"><button type="button" className="secondary" onClick={() => setAdjusting((value) => !value)}>{adjusting ? "收起调整" : "调整频率"}</button><button className="primary" disabled={busy}>{busy ? "保存中…" : "保存访问策略"}</button></footer>
    </form>
  </section>;
}

const CREDENTIAL_LABELS: Record<string, string> = {cookie: "Cookie", passkey: "Passkey", api_key: "API Key"};
const CREDENTIAL_ORDER = ["cookie", "passkey", "api_key"];

function SiteCredentialsEditor({site, catalog, sources, credentials, client, onSaved, onError}: {site: SiteSummary; catalog: AdapterCatalogEnvelope | null; sources: RuleSourceInput[]; credentials: SiteCredential[]; client: ApiClient; onSaved: (credentials: SiteCredential[]) => void; onError: (reason: unknown) => void}) {
  const capability = catalog?.adapters.find((item) => item.kind === "site" && item.site_code === site.code);
  const required = new Set(capability?.credential_fields ?? []);
  if (sources.some((item) => (item.auth_mode ?? "site_cookie") === "site_cookie")) required.add("cookie");
  const fields = [...CREDENTIAL_ORDER.filter((item) => required.has(item)), ...[...required].filter((item) => !CREDENTIAL_ORDER.includes(item)).sort()];
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState("");
  const saveCredential = async (event: FormEvent, field: string) => {
    event.preventDefault();
    const value = values[field]?.trim() ?? "";
    if (!value) return;
    setSaving(field);
    try {
      await client.putSiteCredential(site.code, field, value);
      setValues((current) => ({...current, [field]: ""}));
      onSaved(await client.listSiteCredentials(site.code));
    } catch (reason) { onError(reason); }
    finally { setSaving(""); }
  };
  const profile = fields.length ? fields.map((field) => CREDENTIAL_LABELS[field] ?? field).join(" + ") : "无需凭据";
  return <section id={`site-credentials-${site.code}`} className="rule-side-card site-credentials-editor" aria-labelledby={`site-credentials-title-${site.code}`}>
    <header className="rule-side-card-header"><div><h3 id={`site-credentials-title-${site.code}`}>站点凭据</h3><ContextHelp id={`site-credentials-help-${site.code}`} label="站点凭据说明" text="字段由站点适配器和规则页面的访问方式自动决定。Cookie 请粘贴浏览器请求中的完整 name=value; name=value 内容，也支持 JSON 对象；值会加密保存且不回显、不写日志。Cookie 只发送到明确授权的规则地址，API Key 不会发送给规则页面。" /></div><span className="credential-profile">{profile}</span></header>
    {fields.length ? <div className="site-credential-list">{fields.map((field) => {
      const metadata = credentials.find((item) => item.name === field);
      const label = CREDENTIAL_LABELS[field] ?? field;
      const configured = Boolean(metadata?.enabled);
      return <form className="site-credential-row" key={field} onSubmit={(event) => void saveCredential(event, field)}>
        <label htmlFor={`site-credential-${site.code}-${field}`}>{label}</label><span className={`credential-state ${configured ? "configured" : metadata ? "disabled" : "missing"}`}>{configured ? "已加密保存" : metadata ? "已停用" : "未配置"}</span>
        <input id={`site-credential-${site.code}-${field}`} aria-label={label} type="password" autoComplete="new-password" value={values[field] ?? ""} onChange={(event) => setValues((current) => ({...current, [field]: event.target.value}))} placeholder={configured ? "输入新值可更新" : `输入 ${label}`} />
        <button className="secondary" disabled={saving === field || !(values[field]?.trim())}>{saving === field ? "保存中…" : configured ? "更新" : "保存"}</button>
      </form>;
    })}</div> : <div className="credential-empty">当前站点无需凭据</div>}
  </section>;
}

function ContextHelp({id, label, text}: {id: string; label: string; text: string}) {
  return <span className="context-help"><button type="button" className="info-trigger context-help-trigger" aria-label={label} aria-describedby={id}><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7.5v.5" /></svg></button><span className="context-help-tooltip" id={id} role="tooltip">{text}</span></span>;
}

function ReviewSectionCard({section, revision, client, busy, onBusy, onSaved, onCorrected, onError}: {section: RuleReviewSection; revision: RuleRevision; client: ApiClient; busy: boolean; onBusy: (value: boolean) => void; onSaved: (workspace: RuleReviewWorkspace) => Promise<void>; onCorrected: (revision: RuleRevision, section: string, data: Record<string, JsonValue>, comment: string) => Promise<void>; onError: (reason: unknown) => void}) {
  const initialUploadPolicy = isRecordValue(section.data.upload_policy) ? section.data.upload_policy : {};
  const initialSeedboxPolicy = isRecordValue(section.data.seedbox_upload_policy) ? section.data.seedbox_upload_policy : {};
  const initialDownloadPolicy = isRecordValue(section.data.download_policy) ? section.data.download_policy : {};
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [correctionComment, setCorrectionComment] = useState("");
  const [upload, setUpload] = useState(textValue(section.data.upload, ""));
  const [uploadDeclared, setUploadDeclared] = useState(textValue(initialUploadPolicy.declared, textValue(section.data.upload, "")));
  const [uploadMargin, setUploadMargin] = useState(textValue(initialUploadPolicy.safety_margin, "20MB/s"));
  const [uploadScope, setUploadScope] = useState(textValue(initialUploadPolicy.scope, "per_torrent"));
  const [seedboxUpload, setSeedboxUpload] = useState(textValue(section.data.seedbox_upload, ""));
  const [seedboxDeclared, setSeedboxDeclared] = useState(textValue(initialSeedboxPolicy.declared, textValue(section.data.seedbox_upload, "")));
  const [seedboxMargin, setSeedboxMargin] = useState(textValue(initialSeedboxPolicy.safety_margin, "20MB/s"));
  const [download, setDownload] = useState(textValue(section.data.download, ""));
  const [downloadDeclared, setDownloadDeclared] = useState(textValue(initialDownloadPolicy.declared, textValue(section.data.download, "")));
  const [downloadScope, setDownloadScope] = useState(textValue(initialDownloadPolicy.scope, "per_torrent"));
  const [namingJSON, setNamingJSON] = useState(JSON.stringify(section.data, null, 2));
  const [correctionError, setCorrectionError] = useState("");
  useEffect(() => {
    setUpload(textValue(section.data.upload, ""));
    const uploadPolicy = isRecordValue(section.data.upload_policy) ? section.data.upload_policy : {};
    const seedboxPolicy = isRecordValue(section.data.seedbox_upload_policy) ? section.data.seedbox_upload_policy : {};
    const downloadPolicy = isRecordValue(section.data.download_policy) ? section.data.download_policy : {};
    setUploadDeclared(textValue(uploadPolicy.declared, textValue(section.data.upload, "")));
    setUploadMargin(textValue(uploadPolicy.safety_margin, "20MB/s"));
    setUploadScope(textValue(uploadPolicy.scope, "per_torrent"));
    setSeedboxUpload(textValue(section.data.seedbox_upload, ""));
    setSeedboxDeclared(textValue(seedboxPolicy.declared, textValue(section.data.seedbox_upload, "")));
    setSeedboxMargin(textValue(seedboxPolicy.safety_margin, "20MB/s"));
    setDownload(textValue(section.data.download, ""));
    setDownloadDeclared(textValue(downloadPolicy.declared, textValue(section.data.download, "")));
    setDownloadScope(textValue(downloadPolicy.scope, "per_torrent"));
    setNamingJSON(JSON.stringify(section.data, null, 2));
    setCorrectionComment(""); setCorrectionError(""); setCorrectionOpen(false);
  }, [revision.id, section.key, section.data]);
  const reviewLocked = revision.status !== "draft";
  const save = async (decision: "confirmed" | "needs_changes") => {
    onBusy(true);
    try { await onSaved(await client.reviewRuleSection(revision, section.key, decision, decision === "confirmed" ? "已核对门禁结论" : "门禁需要纠正")); }
    catch (reason) { onError(reason); }
    finally { onBusy(false); }
  };
  const correct = async () => {
    setCorrectionError("");
    let data: Record<string, JsonValue>;
    if (section.key === "upload_limit") data = {
      upload, upload_declared: uploadDeclared, upload_safety_margin: uploadMargin, upload_scope: uploadScope,
      seedbox_upload: seedboxUpload, seedbox_upload_declared: seedboxDeclared, seedbox_upload_safety_margin: seedboxMargin, seedbox_upload_scope: "per_torrent",
    };
    else if (section.key === "download_limit") data = {download, download_declared: downloadDeclared, download_scope: downloadScope};
    else {
      try {
        const parsed = JSON.parse(namingJSON) as JsonValue;
        if (!isRecordValue(parsed)) throw new Error("命名配置必须是 JSON 对象");
        data = parsed as Record<string, JsonValue>;
      } catch (reason) {
        setCorrectionError(reason instanceof Error ? reason.message : "命名配置不是有效 JSON");
        return;
      }
    }
    await onCorrected(revision, section.key, data, correctionComment);
  };
  return <details className={`review-section ${section.check?.decision ?? "pending"}`} open={section.check?.decision === "needs_changes"}>
    <summary><span className="review-state-dot" /><div><strong>{section.title}</strong><small>{section.key} · {statusLabel(section.status)}</small></div><p>{section.summary}</p><b>{section.check?.decision === "confirmed" ? "已确认" : section.check?.decision === "needs_changes" ? "需修改" : "待审核"}</b></summary>
    <div className="review-section-body"><div className="review-facts">{section.facts.map((fact, index) => <article className={`review-fact ${fact.tone ?? "neutral"}`} key={`${fact.label}-${index}`}><span>{fact.label}</span><strong>{fact.value}</strong>{fact.detail && <p>{fact.detail}</p>}</article>)}{!section.facts.length && <p className="review-missing">没有可展示的结构化结论，请对照原始规则全文审核。</p>}<details className="advanced-rule-json"><summary>技术详情</summary><pre>{JSON.stringify(section.data, null, 2)}</pre></details></div><div className="review-section-actions"><button className="secondary" disabled={busy} onClick={() => setCorrectionOpen((value) => !value)}>{correctionOpen ? "取消调整" : "调整门禁"}</button><button className="primary" disabled={busy || reviewLocked} onClick={() => void save("confirmed")}>确认本章节</button></div>{correctionOpen && <section className="hard-gate-correction"><header><strong>调整门禁配置</strong><span>保存会派生新的不可变 revision，并重新审核三项硬门禁。</span></header>{section.key === "upload_limit" ? <div className="hard-gate-correction-grid rate-correction-grid"><label>站点声明上传上限<input value={uploadDeclared} onChange={(event) => setUploadDeclared(event.target.value)} placeholder="例如 125MB/s" /></label><label>安全余量<input value={uploadMargin} onChange={(event) => setUploadMargin(event.target.value)} placeholder="默认 20MB/s" /></label><label>实际单种上传上限<input value={upload} onChange={(event) => setUpload(event.target.value)} placeholder="例如 105MB/s" /></label><label>范围<select value={uploadScope} onChange={(event) => setUploadScope(event.target.value)}><option value="per_torrent">每个种子</option><option value="account_total">账号总量（不可执行）</option><option value="site_total">站点总量（不可执行）</option><option value="unknown">范围未知（不可执行）</option></select></label><label>站点声明盒子上限<input value={seedboxDeclared} onChange={(event) => setSeedboxDeclared(event.target.value)} placeholder="留空表示未声明" /></label><label>盒子安全余量<input value={seedboxMargin} onChange={(event) => setSeedboxMargin(event.target.value)} placeholder="默认 20MB/s" /></label><label>实际盒子单种上限<input value={seedboxUpload} onChange={(event) => setSeedboxUpload(event.target.value)} placeholder="仅标记为 SeedBox 时应用" /></label></div> : section.key === "download_limit" ? <div className="hard-gate-correction-grid rate-correction-grid"><label>站点声明下载上限<input value={downloadDeclared} onChange={(event) => setDownloadDeclared(event.target.value)} placeholder="例如 100MB/s" /></label><label>实际单种下载上限<input value={download} onChange={(event) => setDownload(event.target.value)} placeholder="默认与声明值相同" /></label><label>范围<select value={downloadScope} onChange={(event) => setDownloadScope(event.target.value)}><option value="per_torrent">每个种子</option><option value="account_total">账号总量（不可执行）</option><option value="site_total">站点总量（不可执行）</option><option value="unknown">范围未知（不可执行）</option></select></label></div> : <label>分类命名门禁 JSON<textarea spellCheck={false} value={namingJSON} onChange={(event) => setNamingJSON(event.target.value)} /></label>}<label>调整依据（必填）<textarea value={correctionComment} onChange={(event) => setCorrectionComment(event.target.value)} placeholder="说明原文位置和需要调整的内容" /></label>{correctionError && <p className="review-missing" role="alert">{correctionError}</p>}<button className="primary" disabled={busy || !correctionComment.trim()} onClick={() => void correct()}>保存为新 revision</button></section>}</div>
  </details>;
}

function ObligationCard({item}: {item: Record<string, unknown>}) {
  const description = textValue(item.description, "未填写义务说明");
  const blocking = item.blocking === true;
  const verification = item.verification === "programmatic" ? "程序验证" : "人工核对";
  const resolution = item.resolution === "enforced" ? "已落实" : item.resolution === "not_applicable" ? "不适用" : "待处理";
  const evidence = Array.isArray(item.evidence_refs) ? item.evidence_refs.filter((value): value is string => typeof value === "string") : [];
  return <article className={`obligation-card ${blocking ? "blocking" : "advisory"}`}>
    <header><div><strong>{description}</strong><span>{textValue(item.scope, "适用范围未填写")}</span></div><b>{blocking ? "阻塞义务" : "提示义务"}</b></header>
    <dl><div><dt>核对方式</dt><dd>{verification}</dd></div><div><dt>当前状态</dt><dd>{resolution}</dd></div><div><dt>执行要求</dt><dd>{textValue(item.enforcement, "需人工记录处理方式")}</dd></div></dl>
    {evidence.length > 0 && <p><span>所需证据</span>{evidence.map((value) => <code key={value}>{value}</code>)}</p>}
    <details className="advanced-rule-json"><summary>技术详情</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>
  </article>;
}

function AdvisoryCard({item}: {item: {section: string; severity: string; summary: string; evidence_refs?: string[]}}) {
  return <article className={`obligation-card ${item.severity === "warning" ? "advisory" : ""}`}>
    <header><div><strong>{item.summary}</strong><span>{item.section}</span></div><b>{item.severity === "warning" ? "转种前警告" : "转种前提示"}</b></header>
    {item.evidence_refs?.length ? <p><span>原文依据</span>{item.evidence_refs.map((value) => <code key={value}>{value}</code>)}</p> : null}
  </article>;
}

function SiteEditor({site, client, knownTags, onDirty, onSaved, onError}: {site?: SiteSummary; client: ApiClient; knownTags: string[]; onDirty: () => void; onSaved: (code: string) => Promise<void>; onError: (reason: unknown) => void}) {
  const [form, setForm] = useState(() => siteForm(site));
  const [newTags, setNewTags] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setForm(siteForm(site)), [site]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try {
      const code = form.code.trim().toUpperCase();
      await client.putSite(code, {name: form.name.trim(), adapter: form.adapter, enabled: form.enabled, aliases: splitLabels(form.aliases), tags: [...new Set([...splitLabels(form.tags), ...splitLabels(newTags)])]});
      await onSaved(code);
    } catch (reason) { onError(reason); }
    finally { setBusy(false); }
  };
  const selectedTags = splitLabels(form.tags);
  return <form className="site-editor drawer-form" onSubmit={submit} onChangeCapture={onDirty}><label>显示名称<input required value={form.name} onChange={(event) => setForm({...form, name: event.target.value})} /></label><label>系统代码<input required pattern="[A-Za-z0-9][A-Za-z0-9_-]{1,31}" readOnly={Boolean(site)} value={form.code} onChange={(event) => setForm({...form, code: event.target.value.toUpperCase()})} /></label><label>适配器<select disabled={Boolean(site)} value={form.adapter} onChange={(event) => setForm({...form, adapter: event.target.value})}><option value="config_only">仅配置</option><option value="nexusphp">NexusPHP</option><option value="mteam_api">M-Team API</option><option value="ttg">TTG</option></select></label><label>别名<input value={form.aliases} onChange={(event) => setForm({...form, aliases: event.target.value})} placeholder="多个别名用逗号分隔" /></label>{knownTags.length > 0 && <fieldset className="site-tag-picker"><legend>已有标签</legend>{knownTags.map((value) => <label key={value}><input type="checkbox" checked={selectedTags.includes(value)} onChange={(event) => setForm({...form, tags: event.target.checked ? [...selectedTags, value].join(",") : selectedTags.filter((item) => item !== value).join(",")})}/>{value}</label>)}</fieldset>}<label>新增标签（可选）<input value={newTags} onChange={(event) => setNewTags(event.target.value)} placeholder="多个标签用逗号分隔" /></label><SwitchField checked={form.enabled} onChange={(enabled) => setForm({...form, enabled})} label="启用站点配置"/><footer><button className="primary" disabled={busy}>{busy ? "保存中…" : "保存站点"}</button></footer></form>;
}

function siteForm(site?: SiteSummary) { return {code: site?.code ?? "", name: site?.name ?? "", adapter: site?.adapter ?? "config_only", enabled: site?.enabled ?? true, aliases: site?.aliases?.join(", ") ?? "", tags: site?.tags?.join(", ") ?? ""}; }
function newRuleSource(index: number): RuleSourceInput { return {id: `page-${index + 1}`, url: "", scope: "", auth_mode: "none"}; }
function normalizeRuleSources(sources: RuleSourceInput[]): RuleSourceInput[] { return sources.map((source, index) => ({id: source.id.trim() || `page-${index + 1}`, url: source.url.trim(), scope: source.scope.trim() || `规则页面 ${index + 1}`, auth_mode: source.auth_mode === "none" ? "none" : "site_cookie"})); }
function revisionStatusLabel(revision: RuleRevision, site: SiteSummary) { if (revision.id === site.active_rule_revision_id || revision.fingerprint === site.active_rule_fingerprint) return "当前基准"; if (revision.status === "draft") return "待审核变更"; if (revision.status === "approved") return "已审核，待应用"; return "已归档"; }
function stageLabel(value: WorkbenchStage) { return value === "overview" ? "概览" : value === "sources" ? "规则来源" : value === "review" ? "门禁审核" : "运行配置"; }
function ruleGateSnapshot(revision: RuleRevision, gate: string): string {
  const limits = isRecordValue(revision.policy.limits) ? revision.policy.limits : {};
  if (gate === "upload") return stableRuleJSON({upload: limits.upload, upload_policy: limits.upload_policy, seedbox_upload: limits.seedbox_upload, seedbox_upload_policy: limits.seedbox_upload_policy});
  if (gate === "download") return stableRuleJSON({download: limits.download, download_policy: limits.download_policy});
  return stableRuleJSON(revision.policy.naming);
}
function stableRuleJSON(value: unknown): string { if (Array.isArray(value)) return `[${value.map(stableRuleJSON).join(",")}]`; if (value && typeof value === "object") return `{${Object.entries(value).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => `${JSON.stringify(key)}:${stableRuleJSON(item)}`).join(",")}}`; return JSON.stringify(value) ?? "undefined"; }
function ruleSourceHost(value: string) { try { const parsed = new URL(value); return parsed.protocol === "https:" ? parsed.hostname : ""; } catch { return ""; } }
function collectionStatusLabel(value: string) { return value === "queued" ? "等待访问频率窗口" : value === "fetching" ? "正在逐页采集" : value === "analyzing" ? "模型正在结构化编译" : value === "ready" ? "规则草稿已生成" : value === "failed" ? "本次采集失败" : "尚未开始"; }
function formatBytes(value: number) { return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`; }
function splitLabels(value: string) { return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean); }
function shortHash(value: string) { return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "—"; }
function isRecordValue(value: JsonValue | undefined): value is Record<string, JsonValue> { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
function toActionError(reason: unknown, fallbackCode: string) { const value = reason as {code?: string; message?: string}; return {code: value?.code ?? fallbackCode, message: value?.message ?? "请求未完成"}; }
function ruleCollectionRecovery(code: string, evidenceReady: boolean, timeout?: number) { const prefix=evidenceReady?"规则页面均已成功取证；":""; if(code==="provider_timeout")return `${prefix}模型未在 ${timeout??"配置的"} 秒总预算内完成。可提高超时、降低推理强度或换用更快模型。`; if(code==="provider_response_too_large")return `${prefix}模型响应超过安全上限，与站点访问无关；请查看模型输出规模和完整上下文。`; if(code==="provider_output_truncated"||code==="provider_output_incomplete")return `${prefix}模型没有正常结束结构化输出，系统未尝试修补截断内容。请使用输出预算更充足的模型后重新生成。`; if(code==="provider_configuration_changed")return `${prefix}排队后模型配置发生变化。为避免把原文发送到不同的数据边界，请使用当前配置重新发起采集。`; if(code==="provider_busy")return `${prefix}当前模型分析容量已满，请等待正在运行的分析结束后重试。`; if(code==="rule_source_dns_failed")return "运行容器无法解析该规则页域名；请检查容器 DNS。"; if(code==="rule_source_connection_timeout")return "域名已解析，但运行容器无法在连接预算内建立 TCP 连接；这通常是容器出口路由、防火墙或目标节点不可达。"; if(code==="rule_source_tls_timeout")return "TCP 已连接，但 TLS 握手未在预算内完成；请检查目标节点或 TLS 链路。"; if(code==="rule_source_tls_failed")return "TLS 握手或证书校验失败；请检查系统时间、CA 与目标证书。"; if(code==="site_cookie_invalid")return "Cookie 格式无效；请粘贴浏览器请求中的完整 Cookie，格式为 name=value; name=value。"; if(code.startsWith("provider_")||code.startsWith("rule_draft_"))return `${prefix}站点取证无需重做；请在运维中心按错误码查看模型调用上下文后重新生成。`; return "请检查失败页面的访问方式、地址或访问频率，再手动发起新的采集。"; }
function textValue(value: unknown, fallback: string) { return typeof value === "string" && value.trim() ? value : fallback; }
function statusLabel(value: string) { return value === "extracted" ? "已结构化" : value === "partially_extracted" ? "部分结构化" : value === "not_extracted" ? "未提取，需对照原文" : "原始证据"; }
function Empty({text}: {text: string}) { return <div className="config-empty">{text}</div>; }
