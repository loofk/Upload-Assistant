import type {
  CreateJobInput,
  DailyCandidateSchedule,
  DailyCandidateScheduleListEnvelope,
  DailyCandidateScheduleRunListEnvelope,
  DailyCandidateListEnvelope,
  EventsEnvelope,
  Downloader,
	DownloaderAdapterCapability,
	DownloaderDashboardSnapshot,
	DownloaderTorrentFilesEvidence,
	AdapterCatalogEnvelope,
  ImageHost,
	MediaManager,
	MetadataProvider,
	Notification,
	NotificationChannel,
	JobEnvelope,
	JobAttention,
  JobListEnvelope,
  JobStatus,
  JobSummaryEnvelope,
  NotificationListEnvelope,
	AuditEventListEnvelope,
	LiveReadinessReport,
  JsonValue,
  PathMapping,
  RuleRevision,
  RuleReviewWorkspace,
  ScreenshotProfile,
  SiteCredential,
	SiteAccessPolicy,
	SiteAccessPolicyInput,
  SiteSummary,
	LegacyMigrationEnvelope,
	LegacyMigrationPreview,
	LegacyMigrationRecord,
	StepAttemptListEnvelope,
	UploadPreviewEnvelope,
	OperationalLog,
	OperationalLogListEnvelope,
	LogContext,
	Incident,
	Diagnostic,
	OperationsOverview,
	OperationsSettings,
	BackupRun,
	APITokenRecord,
	LLMProvider,
	ProviderUseCase,
	ProviderReasoningEffort,
	RuleAnalysisResult,
	RuleSourceInput,
	RuleSourceSet,
	RuleCollectionRun,
	BackupPolicy,
} from "./types";

interface ProblemBody {
  code?: string;
  message?: string;
  error?: {code?: string; detail?: string};
  blockers?: Array<{code?: string; message?: string}>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function normalizeRuleReview(review: RuleReviewWorkspace): RuleReviewWorkspace {
  return {
    ...review,
    sections: Array.isArray(review.sections) ? review.sections.map((section) => ({
      ...section,
      facts: Array.isArray(section.facts) ? section.facts : [],
      data: section.data && typeof section.data === "object" ? section.data : {},
    })) : [],
    advisories: Array.isArray(review.advisories) ? review.advisories : [],
    blockers: Array.isArray(review.blockers) ? review.blockers : [],
    next_actions: Array.isArray(review.next_actions) ? review.next_actions : [],
  };
}

function waitForSignal(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason ?? new DOMException("操作已取消", "AbortError"));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, milliseconds);
    const abort = () => { clearTimeout(timer); reject(signal.reason ?? new DOMException("操作已取消", "AbortError")); };
    signal.addEventListener("abort", abort, {once: true});
  });
}

export class ApiClient {
  constructor(private readonly token: string) {}

  async validateToken(): Promise<void> {
    await this.listJobs({limit: 1});
  }

  async listJobs(options: {status?: JobStatus | ""; cursor?: string; limit?: number} = {}): Promise<JobListEnvelope> {
    const query = new URLSearchParams({limit: String(options.limit ?? 25)});
    if (options.status) query.set("status", options.status);
    if (options.cursor) query.set("cursor", options.cursor);
    return this.request(`/api/v2/jobs?${query.toString()}`);
  }

  async listDailyCandidates(options: {source: string; target: string; date?: string; limit?: number}): Promise<DailyCandidateListEnvelope> {
    const query = new URLSearchParams({source: options.source.toUpperCase(), target: options.target.toUpperCase(), limit: String(options.limit ?? 25)});
    if (options.date) query.set("date", options.date);
    return this.request(`/api/v2/candidates/daily?${query.toString()}`);
  }

  async createDailyCandidateJob(input: {source: string; target: string; targetCount: number; scanLimit: number; date?: string}): Promise<JobEnvelope> {
    return this.request("/api/v2/candidates/daily", {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({
        source: input.source.toUpperCase(), target: input.target.toUpperCase(),
        target_count: input.targetCount, scan_limit: input.scanLimit,
        date: input.date || undefined, execution_mode: "auto",
      }),
    });
  }

  async submitDailyCandidate(candidateID: string): Promise<JobEnvelope> {
    return this.request(`/api/v2/candidates/${encodeURIComponent(candidateID)}/retorrent-job`, {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({execution_mode: "step"}),
    });
  }

  async listDailyCandidateSchedules(): Promise<DailyCandidateScheduleListEnvelope> {
    return this.request("/api/v2/schedules/daily-candidates?limit=100");
  }

  async createDailyCandidateSchedule(input: {
    name: string; source: string; target: string; cronExpression: string; timezone: string;
    targetCount?: number; scanLimit?: number; notificationChannels?: string[];
  }): Promise<{ok: true; status: "ready"; schedule_id: string; schedule: DailyCandidateSchedule}> {
    return this.request("/api/v2/schedules/daily-candidates", {
      method: "POST",
      body: JSON.stringify({
        name: input.name, cron_expression: input.cronExpression, timezone: input.timezone, enabled: true,
        config: {source: input.source.toUpperCase(), target: input.target.toUpperCase(), target_count: input.targetCount ?? 10, scan_limit: input.scanLimit ?? 30, page: 1, notification_channels: input.notificationChannels ?? []},
      }),
    });
  }

  async setDailyCandidateScheduleEnabled(scheduleID: string, enabled: boolean): Promise<{ok: true; status: "ready"; schedule_id: string; schedule: DailyCandidateSchedule}> {
    return this.request(`/api/v2/schedules/daily-candidates/${encodeURIComponent(scheduleID)}`, {
      method: "PATCH", body: JSON.stringify({enabled}),
    });
  }

  async listDailyCandidateScheduleRuns(scheduleID: string): Promise<DailyCandidateScheduleRunListEnvelope> {
    return this.request(`/api/v2/schedules/daily-candidates/${encodeURIComponent(scheduleID)}/runs?limit=25`);
  }

  async listNotifications(): Promise<NotificationListEnvelope> {
    return this.request("/api/v2/notifications?limit=25");
  }

  async reconcileNotification(notificationID: string, input: {
    decision: "verified_not_delivered" | "verified_delivered";
    evidenceSHA256: string;
    observedAt: string;
    messageID?: string;
  }): Promise<{ok: true; status: "queued" | "sent"; notification_id: string; notification: Notification}> {
    return this.request(`/api/v2/notifications/${encodeURIComponent(notificationID)}/reconcile`, {
      method: "POST",
      body: JSON.stringify({
        decision: input.decision, confirmed: true, evidence_sha256: input.evidenceSHA256,
        observed_at: input.observedAt, ...(input.messageID ? {message_id: input.messageID} : {}),
      }),
    });
  }

  async listAuditEvents(options: {
    actorType?: string; action?: string; resourceType?: string; resourceID?: string;
    limit?: number; cursor?: string;
  } = {}): Promise<AuditEventListEnvelope> {
    const query = new URLSearchParams({limit: String(options.limit ?? 50)});
    if (options.actorType) query.set("actor_type", options.actorType);
    if (options.action) query.set("action", options.action);
    if (options.resourceType) query.set("resource_type", options.resourceType);
    if (options.resourceID) query.set("resource_id", options.resourceID);
    if (options.cursor) query.set("cursor", options.cursor);
    return this.request(`/api/v2/audit-events?${query.toString()}`);
  }

  async getLiveReadiness(input: {
    source: "U2" | "CHD"; target: "MTEAM"; downloader: string;
    targetDownloader?: string; imageHost: string; screenshotProfile: string;
    tmdbProvider: string; ptgenProvider: string;
  }): Promise<LiveReadinessReport> {
    const query = new URLSearchParams({
      source: input.source, target: input.target, downloader: input.downloader,
      image_host: input.imageHost, screenshot_profile: input.screenshotProfile,
      tmdb_provider: input.tmdbProvider, ptgen_provider: input.ptgenProvider,
    });
    if (input.targetDownloader) query.set("target_downloader", input.targetDownloader);
    return this.request(`/api/v2/readiness/live?${query.toString()}`);
  }

  async getSummary(jobID: string): Promise<JobSummaryEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/summary`);
  }

  async getAttention(jobID: string): Promise<JobAttention> {
    const response = await this.request<{attention: JobAttention}>(`/api/v2/jobs/${encodeURIComponent(jobID)}/attention`);
    return response.attention;
  }

  async performJobAction(jobID: string, input: {actionID: string; expectedStatus: JobStatus; expectedStep?: string; expectedBlockerCode?: string; confirmed?: boolean}): Promise<JobEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/actions`, {
      method: "POST",
      body: JSON.stringify({
        action_id: input.actionID, expected_status: input.expectedStatus, expected_step: input.expectedStep ?? "",
        expected_blocker_code: input.expectedBlockerCode ?? "", confirmed: input.confirmed ?? false,
      }),
    });
  }

  async getUploadPreview(jobID: string): Promise<UploadPreviewEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/upload-preview`);
  }

  async reviseUploadPreview(jobID: string, input: {expectedPackageSHA256: string; name: string; namingProfile: string; smallDescription: string; category: number; categoryEvidence: string; standard: number; anonymous: boolean; description: string}): Promise<void> {
    await this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/upload-preview/revisions`, {
      method: "POST",
      body: JSON.stringify({expected_package_sha256: input.expectedPackageSHA256, fields: {
        name: input.name, naming_profile: input.namingProfile, small_descr: input.smallDescription, category: input.category,
        category_evidence: input.categoryEvidence, standard: input.standard,
        anonymous: input.anonymous, description: input.description,
      }}),
    });
  }

  async getEvents(jobID: string): Promise<EventsEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/events?after=0&limit=500`);
  }

  async getAttempts(jobID: string, cursor = ""): Promise<StepAttemptListEnvelope> {
    const query = new URLSearchParams({limit: "500"});
    if (cursor) query.set("cursor", cursor);
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/attempts?${query.toString()}`);
  }

  async createJob(input: CreateJobInput): Promise<JobEnvelope> {
		const downloader: Record<string, JsonValue> = {
			name: input.downloaderName,
			save_path: input.savePath,
			apply_labels: input.applyLabels,
			skip_checking: false,
			paused: false,
		};
		if (input.applyLabels) {
			downloader.category = "retorrent-source";
			downloader.tags = ["upload-assistant", "source"];
		}
    const workflowInput: Record<string, JsonValue> = {
      source_url: input.sourceURL,
      target: input.target.toUpperCase(),
      confirm_upload: false,
			downloader,
			target_downloader: {apply_labels: input.applyLabels},
      screenshots: {profile: input.screenshotProfile},
      image_host: {name: input.imageHost},
    };
    if (input.tmdbProvider || input.ptgenProvider) {
      const providers: Record<string, JsonValue> = {};
      if (input.tmdbProvider) providers.tmdb = input.tmdbProvider;
      if (input.ptgenProvider) providers.ptgen = input.ptgenProvider;
      workflowInput.metadata_providers = providers;
    }
    const body: Record<string, JsonValue> = {
      kind: "retorrent",
      execution_mode: input.executionMode,
      input: workflowInput,
    };
    if (input.stopAfterStep) body.stop_after_step = input.stopAfterStep;
    return this.request("/api/v2/jobs", {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify(body),
    });
  }

  async transition(jobID: string, action: "pause" | "cancel"): Promise<JobEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/${action}`, {method: "POST"});
  }

  async resume(jobID: string, resumeState: Record<string, JsonValue>): Promise<JobEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/resume`, {
      method: "POST",
      body: JSON.stringify({resume_state: resumeState}),
    });
  }

  async replayJob(jobID: string): Promise<JobEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/replay`, {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({execution_mode: "step"}),
    });
  }

  async downloadArtifact(jobID: string, artifactID: string): Promise<Blob> {
    const response = await fetch(`/api/v2/jobs/${encodeURIComponent(jobID)}/artifacts/${encodeURIComponent(artifactID)}/content`, {
      headers: {Authorization: `Bearer ${this.token}`, Accept: "application/octet-stream"},
      credentials: "same-origin",
    });
    if (!response.ok) await this.throwResponseError(response);
    return response.blob();
  }

  async listDownloaders(): Promise<Downloader[]> {
    const response = await this.request<{downloaders: Downloader[]}>("/api/v2/downloaders");
    return response.downloaders;
  }

  async listDownloaderAdapters(): Promise<DownloaderAdapterCapability[]> {
    const response = await this.request<{adapters: DownloaderAdapterCapability[]}>("/api/v2/downloader-adapters");
    return response.adapters;
  }

  async listAdapterCapabilities(): Promise<AdapterCatalogEnvelope> {
    return this.request("/api/v2/adapters");
  }

  async putDownloader(name: string, input: {
    adapter: string;
    endpoint: string;
    credentials: Record<string, string>;
		pathMappings: PathMapping[];
		enabled: boolean;
		networkClass: "unknown" | "home" | "seedbox";
  }): Promise<void> {
    await this.request(`/api/v2/downloaders/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
			adapter: input.adapter, enabled: input.enabled, network_class: input.networkClass,
        config: {endpoint: input.endpoint, timeout_seconds: 30, options: {}},
        credentials: input.credentials, path_mappings: input.pathMappings,
      }),
    });
  }

  async probeDownloader(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/downloaders/${encodeURIComponent(name)}/probe`, {method: "POST"});
  }

	async getDownloaderSnapshot(name: string, filters: {filter?: string; query?: string; offset?: number; limit?: number} = {}): Promise<DownloaderDashboardSnapshot> {
		const query = new URLSearchParams();
		if (filters.filter && filters.filter !== "all") query.set("filter", filters.filter);
		if (filters.query) query.set("query", filters.query);
		if (filters.offset) query.set("offset", String(filters.offset));
		if (filters.limit) query.set("limit", String(filters.limit));
		const suffix = query.size ? `?${query}` : "";
		const response = await this.request<{snapshot: DownloaderDashboardSnapshot}>(`/api/v2/downloaders/${encodeURIComponent(name)}/snapshot${suffix}`);
		return response.snapshot;
	}

	async getDownloaderTorrentFiles(name: string, hash: string): Promise<DownloaderTorrentFilesEvidence> {
		const response = await this.request<{evidence: DownloaderTorrentFilesEvidence}>(`/api/v2/downloaders/${encodeURIComponent(name)}/torrents/${encodeURIComponent(hash)}/files`);
		return response.evidence;
	}

  async listImageHosts(): Promise<ImageHost[]> {
    const response = await this.request<{image_hosts: ImageHost[]}>("/api/v2/image-hosts");
    return response.image_hosts;
  }

  async probeImageHost(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/image-hosts/${encodeURIComponent(name)}/probe`, {
      method: "POST", body: JSON.stringify({confirm_upload: true}),
    });
  }

  async listNotificationChannels(): Promise<NotificationChannel[]> {
    const response = await this.request<{notification_channels: NotificationChannel[]}>("/api/v2/notification-channels");
    return response.notification_channels;
  }

  async probeNotificationChannel(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/notification-channels/${encodeURIComponent(name)}/probe`, {
      method: "POST", body: JSON.stringify({confirm_delivery: true}),
    });
  }

  async putNotificationChannel(name: string, input: {adapter: NotificationChannel["adapter"]; enabled: boolean; webhookURL: string; botToken: string; chatID: string; eventTypes: string[]}): Promise<void> {
    const credentials = input.adapter === "telegram_bot"
      ? (input.botToken || input.chatID ? {bot_token: input.botToken, chat_id: input.chatID} : {})
      : (input.webhookURL ? {webhook_url: input.webhookURL} : {});
    await this.request(`/api/v2/notification-channels/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({adapter: input.adapter, enabled: input.enabled, config: {timeout_seconds: 15, event_types: input.eventTypes, options: {}}, credentials}),
    });
  }

  async listMediaManagers(): Promise<MediaManager[]> {
    const response = await this.request<{media_managers: MediaManager[]}>("/api/v2/media-managers");
    return response.media_managers;
  }

  async putMediaManager(name: string, input: {adapter: "sonarr" | "radarr"; enabled: boolean; endpoint: string; apiKey: string}): Promise<void> {
    await this.request(`/api/v2/media-managers/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({adapter: input.adapter, enabled: input.enabled, config: {endpoint: input.endpoint, timeout_seconds: 15, options: {}}, credentials: input.apiKey ? {api_key: input.apiKey} : {}}),
    });
  }

  async probeMediaManager(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/media-managers/${encodeURIComponent(name)}/probe`, {method: "POST"});
  }

  async listMetadataProviders(): Promise<MetadataProvider[]> {
    const response = await this.request<{metadata_providers: MetadataProvider[]}>('/api/v2/metadata-providers');
    return response.metadata_providers;
  }

  async probeMetadataProvider(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/metadata-providers/${encodeURIComponent(name)}/probe`, {method: "POST"});
  }

  async putMetadataProvider(name: string, input: {adapter: "tmdb" | "ptgen"; enabled: boolean; endpoint: string; apiKey: string}): Promise<void> {
    await this.request(`/api/v2/metadata-providers/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({adapter: input.adapter, enabled: input.enabled, config: {endpoint: input.endpoint, timeout_seconds: 30, options: {}}, credentials: input.apiKey ? {api_key: input.apiKey} : {}}),
    });
  }

  async putImageHost(name: string, input: {adapter: string; endpoint: string; apiKey: string; priority: number; enabled?: boolean}): Promise<void> {
		const credentials = (input.adapter === "imgbb" || input.adapter === "ptpimg") && input.apiKey ? {api_key: input.apiKey} : {};
    await this.request(`/api/v2/image-hosts/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
        adapter: input.adapter, enabled: input.enabled ?? true, priority: input.priority,
        config: {endpoint: input.endpoint, timeout_seconds: 30, options: {}},
				credentials,
      }),
    });
  }

  async listScreenshotProfiles(): Promise<ScreenshotProfile[]> {
    const response = await this.request<{screenshot_profiles: ScreenshotProfile[]}>("/api/v2/screenshot-profiles");
    return response.screenshot_profiles;
  }

  async createScreenshotProfile(input: {name: string; count: number; format: string; width: number; quality: number; startPercent: number; endPercent: number}): Promise<void> {
    await this.request("/api/v2/screenshot-profiles", {
      method: "POST",
      body: JSON.stringify({name: input.name, enabled: true, config: {
        count: input.count, format: input.format, width: input.width, quality: input.quality,
        start_percent: input.startPercent, end_percent: input.endPercent, comparison: false,
      }}),
    });
  }

  async listSites(): Promise<SiteSummary[]> {
    const response = await this.request<{sites: SiteSummary[]}>("/api/v2/sites");
    return response.sites;
  }

  async putSite(code: string, input: {name: string; adapter: string; enabled: boolean; aliases: string[]; tags: string[]}): Promise<SiteSummary> {
    const response = await this.request<{site: SiteSummary}>(`/api/v2/sites/${encodeURIComponent(code.toUpperCase())}`, {
      method: "PUT", body: JSON.stringify(input),
    });
    return response.site;
  }

  async listRuleRevisions(siteCode: string): Promise<RuleRevision[]> {
    const response = await this.request<{revisions: RuleRevision[]}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/rules`);
    return response.revisions;
  }

  async importRuleMarkdown(markdown: string): Promise<RuleRevision> {
    const response = await this.request<{revision: RuleRevision}>("/api/v2/site-rules/import", {
      method: "POST", body: JSON.stringify({markdown}),
    });
    return response.revision;
  }

  async approveRule(revision: RuleRevision, comment: string): Promise<void> {
    await this.request(`/api/v2/site-rules/${encodeURIComponent(revision.id)}/approve`, {
      method: "POST", body: JSON.stringify({fingerprint: revision.fingerprint, comment}),
    });
  }

  async activateRule(revisionID: string): Promise<void> {
    await this.request(`/api/v2/site-rules/${encodeURIComponent(revisionID)}/activate`, {method: "POST"});
  }

  async discardRuleDraft(revision: RuleRevision): Promise<void> {
    await this.request(`/api/v2/site-rules/${encodeURIComponent(revision.id)}/discard`, {
      method: "POST", body: JSON.stringify({fingerprint: revision.fingerprint, confirm: true}),
    });
  }

  async getRuleMarkdown(revisionID: string): Promise<string> {
    const response = await fetch(`/api/v2/site-rules/${encodeURIComponent(revisionID)}/markdown`, {
      headers: {Authorization: `Bearer ${this.token}`, Accept: "text/markdown"}, credentials: "same-origin",
    });
    if (!response.ok) await this.throwResponseError(response);
    return response.text();
  }

  async getRuleReview(revisionID: string): Promise<RuleReviewWorkspace> {
    const response = await this.request<{review: RuleReviewWorkspace}>(`/api/v2/site-rules/${encodeURIComponent(revisionID)}/review`);
    return normalizeRuleReview(response.review);
  }

  async reviewRuleSection(revision: RuleRevision, section: string, decision: "confirmed" | "needs_changes", comment: string): Promise<RuleReviewWorkspace> {
    const response = await this.request<{review: RuleReviewWorkspace}>(`/api/v2/site-rules/${encodeURIComponent(revision.id)}/review/${encodeURIComponent(section)}`, {
      method: "PUT", body: JSON.stringify({fingerprint: revision.fingerprint, decision, comment}),
    });
    return normalizeRuleReview(response.review);
  }

  async correctRuleHardGate(revision: RuleRevision, section: string, data: Record<string, JsonValue>, comment: string): Promise<RuleRevision> {
    const response = await this.request<{revision: RuleRevision}>(`/api/v2/site-rules/${encodeURIComponent(revision.id)}/corrections/${encodeURIComponent(section)}`, {
      method: "POST", body: JSON.stringify({fingerprint: revision.fingerprint, data, comment}),
    });
    return response.revision;
  }

  async listSiteCredentials(siteCode: string): Promise<SiteCredential[]> {
    const response = await this.request<{credentials: SiteCredential[]}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/credentials`);
    return response.credentials;
  }

  async putSiteCredential(siteCode: string, name: string, value: string): Promise<void> {
    await this.request(`/api/v2/sites/${encodeURIComponent(siteCode)}/credentials/${encodeURIComponent(name)}`, {
      method: "PUT", body: JSON.stringify({value}),
    });
  }

  async getSiteAccessPolicy(siteCode: string): Promise<SiteAccessPolicy> {
    const response = await this.request<{access_policy: SiteAccessPolicy}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/access-policy`);
    return response.access_policy;
  }

  async putSiteAccessPolicy(siteCode: string, input: SiteAccessPolicyInput): Promise<SiteAccessPolicy> {
    const response = await this.request<{access_policy: SiteAccessPolicy}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/access-policy`, {
      method: "PUT", body: JSON.stringify(input),
    });
    return response.access_policy;
  }

  async getRuleSourceSet(siteCode: string): Promise<RuleSourceSet> {
    const response = await this.request<{source_set: RuleSourceSet}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/rule-sources`);
    const sourceSet = response.source_set ?? {site_code: siteCode.toUpperCase(), sources: [], fingerprint: "", scope_confirmed: false, cookie_hosts_confirmed: false, cookie_configured: false, cookie_required: false};
    sourceSet.sources = sourceSet.sources.map((source) => ({...source, auth_mode: source.auth_mode === "none" ? "none" : "site_cookie"}));
    sourceSet.cookie_required = sourceSet.cookie_required ?? sourceSet.sources.some((source) => source.auth_mode === "site_cookie");
    return sourceSet;
  }

  async putRuleSourceSet(siteCode: string, input: {sources: RuleSourceInput[]; scope_confirmed: boolean; cookie_hosts_confirmed: boolean}): Promise<RuleSourceSet> {
    const response = await this.request<{source_set: RuleSourceSet}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/rule-sources`, {
      method: "PUT", body: JSON.stringify(input),
    });
    return response.source_set;
  }

  async latestRuleCollectionRun(siteCode: string): Promise<RuleCollectionRun | null> {
    const response = await this.request<{status: string; run?: RuleCollectionRun}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/rule-collection-runs/latest`);
    return response.status === "empty" ? null : response.run ?? null;
  }

  async createRuleCollectionRun(siteCode: string, sourceSetFingerprint: string, providerID: string): Promise<RuleCollectionRun> {
    const response = await this.request<{run: RuleCollectionRun}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/rule-collection-runs`, {
      method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({source_set_fingerprint: sourceSetFingerprint, provider_id: providerID, confirm: true}),
    });
    return response.run;
  }

  async getRuleCollectionRun(runID: string): Promise<RuleCollectionRun> {
    const response = await this.request<{run: RuleCollectionRun}>(`/api/v2/site-rule-collection-runs/${encodeURIComponent(runID)}`);
    return response.run;
  }

  async streamRuleCollectionRun(runID: string, onProgress: (run: RuleCollectionRun) => void, signal: AbortSignal): Promise<void> {
    let terminal = false;
    const observe = (run: RuleCollectionRun) => {
      onProgress(run);
      terminal = run.status === "ready" || run.status === "failed";
    };
    try {
      const response = await fetch(`/api/v2/site-rule-collection-runs/${encodeURIComponent(runID)}/stream`, {
        headers: {Authorization: `Bearer ${this.token}`, Accept: "text/event-stream"}, credentials: "same-origin", signal,
      });
      if (!response.ok) await this.throwResponseError(response);
      if (!response.body) throw new ApiError(500, "stream_unavailable", "规则采集状态流不可用");
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = "";
      const consume = (block: string) => {
        let event = ""; const data: string[] = [];
        for (const line of block.split(/\r?\n/)) { if (line.startsWith("event:")) event = line.slice(6).trim(); else if (line.startsWith("data:")) data.push(line.slice(5).trimStart()); }
        if (event === "progress" && data.length) { const payload = JSON.parse(data.join("\n")) as {run?: RuleCollectionRun}; if (payload.run) observe(payload.run); }
      };
      while (!terminal) { const {done, value} = await reader.read(); pending += decoder.decode(value, {stream: !done}); const blocks = pending.split(/\r?\n\r?\n/); pending = blocks.pop() ?? ""; for (const block of blocks) consume(block); if (done) { if (pending.trim()) consume(pending); break; } }
      if (terminal) return;
    } catch (reason) {
      if (signal.aborted) throw reason;
      if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500) throw reason;
    }

    // A browser-facing proxy or the server write deadline may end a healthy SSE
    // connection before a long model call finishes. Continue observing the same
    // durable run; never turn a status-stream interruption into a second run.
    let consecutiveFailures = 0;
    while (!terminal) {
      if (signal.aborted) throw signal.reason ?? new DOMException("操作已取消", "AbortError");
      try {
        observe(await this.getRuleCollectionRun(runID));
        consecutiveFailures = 0;
      } catch (reason) {
        if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500) throw reason;
        consecutiveFailures++;
        if (consecutiveFailures >= 3) throw new ApiError(502, "rule_collection_status_unavailable", reason instanceof Error ? reason.message : "规则采集状态暂时不可用");
      }
      if (!terminal) await waitForSignal(1500, signal);
    }
  }

  async previewLegacyMigration(): Promise<LegacyMigrationPreview> {
    return this.request("/api/v2/migrations/legacy/preview");
  }

  async executeLegacyMigration(sourceFingerprint: string): Promise<LegacyMigrationEnvelope> {
    return this.request("/api/v2/migrations/legacy", {
      method: "POST",
      body: JSON.stringify({source_fingerprint: sourceFingerprint, confirm_import: true}),
    });
  }

  async listLegacyMigrations(): Promise<LegacyMigrationRecord[]> {
    const response = await this.request<{imports: LegacyMigrationRecord[]}>("/api/v2/migrations/legacy?limit=25");
    return response.imports;
  }

  async getOperationsOverview(): Promise<OperationsOverview> {const response=await this.request<{overview:OperationsOverview}>("/api/v2/operations/overview");return response.overview;}
  async getOperationsSettings():Promise<OperationsSettings>{const response=await this.request<{settings:OperationsSettings}>("/api/v2/operations/settings");return response.settings;}
  async putOperationsSettings(settings:OperationsSettings):Promise<OperationsSettings>{const response=await this.request<{settings:OperationsSettings}>("/api/v2/operations/settings",{method:"PUT",body:JSON.stringify(settings)});return response.settings;}
  async listOperationalLogs(options:{level?:string;query?:string;component?:string;errorCode?:string;statusCode?:number;jobID?:string;from?:string;to?:string;cursor?:string;limit?:number}={}):Promise<OperationalLogListEnvelope>{return this.request(`/api/v2/operational-logs?${this.operationalLogQuery(options)}`);}
  async streamOperationalLogs(options:{level?:string;query?:string;component?:string;errorCode?:string;statusCode?:number;from?:string;to?:string;afterID?:number},onLog:(entry:OperationalLog)=>void,signal:AbortSignal):Promise<void>{
    const headers=new Headers({Authorization:`Bearer ${this.token}`,Accept:"text/event-stream"});
    if(options.afterID)headers.set("Last-Event-ID",String(options.afterID));
    const response=await fetch(`/api/v2/operational-logs/stream?${this.operationalLogQuery({...options,limit:200})}`,{headers,credentials:"same-origin",signal});
    if(!response.ok)await this.throwResponseError(response);
    if(!response.body)throw new ApiError(500,"stream_unavailable","日志实时流不可用");
    const reader=response.body.getReader();const decoder=new TextDecoder();let pending="";
    const consume=(block:string)=>{let event="";const data:string[]=[];for(const line of block.split(/\r?\n/)){if(line.startsWith("event:"))event=line.slice(6).trim();else if(line.startsWith("data:"))data.push(line.slice(5).trimStart())}if(event==="operational-log"&&data.length){onLog(JSON.parse(data.join("\n")) as OperationalLog)}};
    while(true){const {done,value}=await reader.read();pending+=decoder.decode(value,{stream:!done});const blocks=pending.split(/\r?\n\r?\n/);pending=blocks.pop()??"";for(const block of blocks)consume(block);if(done){if(pending.trim())consume(pending);return}}
  }
  async getOperationalLogContext(logID:number):Promise<LogContext>{const response=await this.request<{context:LogContext}>(`/api/v2/operational-logs/${encodeURIComponent(String(logID))}/context`);return response.context;}
  async listIncidents(status=""):Promise<Incident[]>{const query=new URLSearchParams({limit:"100"});if(status)query.set("status",status);const response=await this.request<{incidents:Incident[]}>(`/api/v2/incidents?${query}`);return response.incidents;}
  async setIncidentStatus(id:string,status:"acknowledge"|"resolve"):Promise<void>{await this.request(`/api/v2/incidents/${encodeURIComponent(id)}/${status}`,{method:"POST"});}
  async listDiagnostics():Promise<Diagnostic[]>{const response=await this.request<{diagnostics:Diagnostic[]}>("/api/v2/diagnostics?limit=100");return response.diagnostics;}
  async listLLMProviders():Promise<LLMProvider[]>{const response=await this.request<{llm_providers:LLMProvider[]}>("/api/v2/llm-providers");return response.llm_providers;}
  async putLLMProvider(id:string,input:{name:string;baseURL:string;model:string;dataLevel:"local"|"remote";apiMode:"chat_completions"|"responses";reasoningEffort:ProviderReasoningEffort;useCases:ProviderUseCase[];jsonMode:boolean;streamingEnabled:boolean;timeoutSeconds:number;enabled:boolean;outboundConsent:boolean;apiKey?:string}):Promise<LLMProvider>{const response=await this.request<{llm_provider:LLMProvider}>(`/api/v2/llm-providers/${encodeURIComponent(id)}`,{method:"PUT",body:JSON.stringify({name:input.name,base_url:input.baseURL,model:input.model,data_level:input.dataLevel,api_mode:input.apiMode,reasoning_effort:input.reasoningEffort,use_cases:input.useCases,json_mode:input.jsonMode,streaming_enabled:input.streamingEnabled,timeout_seconds:input.timeoutSeconds,enabled:input.enabled,outbound_consent:input.outboundConsent,api_key:input.apiKey??""})});return response.llm_provider;}
  async probeLLMProvider(id:string,stage:"catalog"|"inference"):Promise<LLMProvider>{const response=await this.request<{llm_provider:LLMProvider}>(`/api/v2/llm-providers/${encodeURIComponent(id)}/probe?stage=${stage}`,{method:"POST"});return response.llm_provider;}
  async analyzeRuleText(input:{providerID:string;sourceRevisionID?:string;siteCode?:string;displayName?:string;roles?:Array<"source"|"target">;sourceURL?:string;sourceScope?:string;sourceComplete?:boolean;sourceText?:string}):Promise<RuleAnalysisResult>{
    const idempotencyKey=crypto.randomUUID();
    try{return await this.streamRuleAnalysis(input,idempotencyKey)}catch(reason){
      if(reason instanceof ApiError&&reason.code!=="rule_analysis_stream_interrupted"&&reason.code!=="provider_stream_incomplete"&&reason.code!=="stream_unavailable")throw reason;
      return this.pollRuleAnalysisResult(idempotencyKey);
    }
  }
  async createDiagnostic(input:{providerID:string;jobID?:string;incidentID?:string;logID?:number}):Promise<Diagnostic>{const response=await this.request<{diagnostic:Diagnostic}>("/api/v2/diagnostics",{method:"POST",body:JSON.stringify({provider_id:input.providerID,job_id:input.jobID??"",incident_id:input.incidentID??"",log_id:input.logID??0})});return response.diagnostic;}
  async listBackupRuns():Promise<BackupRun[]>{const response=await this.request<{backup_runs:BackupRun[]}>("/api/v2/backups/runs");return response.backup_runs;}
  async getBackupPolicy():Promise<BackupPolicy>{const response=await this.request<{policy:BackupPolicy}>("/api/v2/backups/policy");return response.policy;}
  async putBackupPolicy(input:{enabled:boolean;recipient:string;schedule:string;retentionCount:number;generateIdentity?:boolean}):Promise<{policy:BackupPolicy;identity_once?:string}>{return this.request("/api/v2/backups/policy",{method:"PUT",body:JSON.stringify({enabled:input.enabled,recipient:input.recipient,schedule:input.schedule,retention_count:input.retentionCount,generate_identity:input.generateIdentity??false})});}
  async createBackup():Promise<BackupRun>{const response=await this.request<{backup:BackupRun}>("/api/v2/backups",{method:"POST"});return response.backup;}
  async verifyBackup(id:string):Promise<BackupRun>{const response=await this.request<{backup:BackupRun}>(`/api/v2/backups/${encodeURIComponent(id)}/verify`,{method:"POST"});return response.backup;}
  async listAPITokens():Promise<APITokenRecord[]>{const response=await this.request<{api_tokens:APITokenRecord[]}>("/api/v2/api-tokens");return response.api_tokens;}
  async createAPIToken(input:{name:string;scopes:string[];expiresInDays:number}):Promise<APITokenRecord>{const response=await this.request<{api_token:APITokenRecord}>("/api/v2/api-tokens",{method:"POST",body:JSON.stringify({name:input.name,scopes:input.scopes,expires_in_days:input.expiresInDays})});return response.api_token;}
  async revokeAPIToken(id:string):Promise<void>{await this.request(`/api/v2/api-tokens/${encodeURIComponent(id)}`,{method:"DELETE"});}

  private operationalLogQuery(options:{level?:string;query?:string;component?:string;errorCode?:string;statusCode?:number;jobID?:string;from?:string;to?:string;cursor?:string;limit?:number}):string{const query=new URLSearchParams({limit:String(options.limit??100)});if(options.level)query.set("level",options.level);if(options.query)query.set("q",options.query);if(options.component)query.set("component",options.component);if(options.errorCode)query.set("error_code",options.errorCode);if(options.statusCode)query.set("status_code",String(options.statusCode));if(options.jobID)query.set("job_id",options.jobID);if(options.from)query.set("from",options.from);if(options.to)query.set("to",options.to);if(options.cursor)query.set("cursor",options.cursor);return query.toString()}

  private async streamRuleAnalysis(input:{providerID:string;sourceRevisionID?:string;siteCode?:string;displayName?:string;roles?:Array<"source"|"target">;sourceURL?:string;sourceScope?:string;sourceComplete?:boolean;sourceText?:string},idempotencyKey:string):Promise<RuleAnalysisResult>{
    const headers=new Headers({Authorization:`Bearer ${this.token}`,Accept:"text/event-stream","Content-Type":"application/json","Idempotency-Key":idempotencyKey});
    let response:Response;try{response=await fetch("/api/v2/site-rules/analyze/stream",{method:"POST",headers,credentials:"same-origin",body:JSON.stringify({provider_id:input.providerID,source_revision_id:input.sourceRevisionID??"",site_code:input.siteCode??"",display_name:input.displayName??"",roles:input.roles??[],source_url:input.sourceURL??"",source_scope:input.sourceScope??"",source_complete:input.sourceComplete??false,source_text:input.sourceText??""})})}catch(reason){throw new ApiError(502,"rule_analysis_stream_interrupted",reason instanceof Error?reason.message:"规则分析连接中断")}
    if(!response.ok)await this.throwResponseError(response);
    if(!response.body)throw new ApiError(500,"stream_unavailable","规则分析流不可用");
    const reader=response.body.getReader();const decoder=new TextDecoder();let pending="";let result:RuleAnalysisResult|undefined;
    const consume=(block:string)=>{let event="";const data:string[]=[];for(const line of block.split(/\r?\n/)){if(line.startsWith("event:"))event=line.slice(6).trim();else if(line.startsWith("data:"))data.push(line.slice(5).trimStart())}if(!data.length)return;const payload=JSON.parse(data.join("\n")) as {analysis?:RuleAnalysisResult;http_status?:number;error?:{code?:string;detail?:string};blockers?:Array<{code?:string;message?:string}>};if(event==="analysis-result"&&payload.analysis)result=payload.analysis;else if(event==="analysis-error"){const code=payload.error?.code??payload.blockers?.[0]?.code??"rule_analysis_failed";const detail=payload.error?.detail??payload.blockers?.[0]?.message??"规则分析失败";throw new ApiError(payload.http_status??502,code,detail)}};
    try{while(true){const {done,value}=await reader.read();pending+=decoder.decode(value,{stream:!done});const blocks=pending.split(/\r?\n\r?\n/);pending=blocks.pop()??"";for(const block of blocks)consume(block);if(result)return result;if(done){if(pending.trim())consume(pending);if(result)return result;throw new ApiError(502,"provider_stream_incomplete","规则分析连接已结束，但没有返回分析结果")}}}catch(reason){if(reason instanceof ApiError)throw reason;throw new ApiError(502,"rule_analysis_stream_interrupted",reason instanceof Error?reason.message:"规则分析连接中断")}
  }

  private async pollRuleAnalysisResult(idempotencyKey:string):Promise<RuleAnalysisResult>{
    const deadline=Date.now()+11*60*1000;let lastNetworkError="";
    while(Date.now()<deadline){await new Promise((resolve)=>setTimeout(resolve,1500));const headers=new Headers({Authorization:`Bearer ${this.token}`,Accept:"application/json","Idempotency-Key":idempotencyKey});let response:Response;try{response=await fetch("/api/v2/site-rules/analyze/result",{headers,credentials:"same-origin"})}catch(reason){lastNetworkError=reason instanceof Error?reason.message:"network error";continue}if(response.status===202)continue;if(!response.ok)await this.throwResponseError(response);const payload=await response.json() as {analysis?:RuleAnalysisResult};if(payload.analysis)return payload.analysis;throw new ApiError(502,"analysis_result_invalid","规则分析结果缺少 analysis 字段")
    }
    throw new ApiError(504,"analysis_result_poll_timeout",lastNetworkError?`等待规则分析结果超时：${lastNetworkError}`:"等待规则分析结果超时");
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {...init, headers, credentials: "same-origin"});
    if (!response.ok) await this.throwResponseError(response);
    return (await response.json()) as T;
  }

  private async throwResponseError(response: Response): Promise<never> {
    let problem: ProblemBody = {};
    try {
      problem = (await response.json()) as ProblemBody;
    } catch {
      // Keep a bounded generic error when a proxy returns non-JSON.
    }
    const code = problem.error?.code ?? problem.code ?? problem.blockers?.[0]?.code ?? "request_failed";
    const detail = problem.error?.detail ?? problem.message ?? problem.blockers?.[0]?.message ?? `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, code, detail);
  }
}
