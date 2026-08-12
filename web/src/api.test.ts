import {afterEach, describe, expect, it, vi} from "vitest";
import {ApiClient} from "./api";

describe("ApiClient safety defaults", () => {
	it("validates a Web token through the protected jobs boundary", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", jobs: [], has_more: false, next_cursor: "",
		}), {status: 200, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		await new ApiClient("ua_test-token-value-that-is-long-enough").validateToken();
		const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(path).toBe("/api/v2/jobs?limit=1");
		expect(new Headers(init.headers).get("Authorization")).toBe("Bearer ua_test-token-value-that-is-long-enough");
	});

	it("reads the local fingerprinted adapter catalog without external intent", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", catalog_version: "upload-assistant.adapter-catalog.v1",
			catalog_sha256: "a".repeat(64), count: 0, adapters: [], blockers: [], next_actions: [],
		}), {status: 200, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		await new ApiClient("ua_test-token-value-that-is-long-enough").listAdapterCapabilities();
		const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(path).toBe("/api/v2/adapters");
		expect(init.method).toBeUndefined();
		expect(init.body).toBeUndefined();
	});

	it("reads a bounded downloader snapshot without sending a write request", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			ok: true, status: "ready", snapshot: {downloader_name: "box", torrents: [], filtered_total: 0}, blockers: [], next_actions: [],
		}), {status: 200, headers: {"Content-Type": "application/json"}}));
		vi.stubGlobal("fetch", fetchMock);
		await new ApiClient("ua_test-token-value-that-is-long-enough").getDownloaderSnapshot("box", {filter: "active", query: "MTEAM", offset: 100, limit: 100});
		const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(path).toBe("/api/v2/downloaders/box/snapshot?filter=active&query=MTEAM&offset=100&limit=100");
		expect(init.method).toBeUndefined();
		expect(init.body).toBeUndefined();
	});

	it("configures keyless image hosts without sending an API key field", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ok: true, status: "configured"}), {
			status: 200, headers: {"Content-Type": "application/json"},
		}));
		vi.stubGlobal("fetch", fetchMock);
		const client = new ApiClient("ua_test-token-value-that-is-long-enough");
		await client.putImageHost("pixhost-main", {
			adapter: "pixhost", endpoint: "https://api.pixhost.to/images", apiKey: "must-not-be-sent", priority: 100, enabled: true,
		});
		const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(path).toBe("/api/v2/image-hosts/pixhost-main");
		const body = JSON.parse(String(init.body));
		expect(body).toMatchObject({adapter: "pixhost", config: {endpoint: "https://api.pixhost.to/images"}, credentials: {}});
		expect(String(init.body)).not.toContain("must-not-be-sent");
	});

	it("sends explicit confirmation only for probes that create remote content", async () => {
		const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ok: true, status: "ready"}), {
			status: 200, headers: {"Content-Type": "application/json"},
		})));
		vi.stubGlobal("fetch", fetchMock);
		const client = new ApiClient("ua_test-token-value-that-is-long-enough");
		await client.probeImageHost("images/main");
		await client.probeNotificationChannel("alerts/main");
		await client.probeMetadataProvider("tmdb/main");
		expect(fetchMock.mock.calls.map(([path, init]) => [path, JSON.parse(String((init as RequestInit).body ?? "{}"))])).toEqual([
			["/api/v2/image-hosts/images%2Fmain/probe", {confirm_upload: true}],
			["/api/v2/notification-channels/alerts%2Fmain/probe", {confirm_delivery: true}],
			["/api/v2/metadata-providers/tmdb%2Fmain/probe", {}],
		]);
	});

  afterEach(() => {vi.useRealTimers();vi.unstubAllGlobals();});

  it("authenticates requests but never infers rule acceptance or live upload confirmation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ok: true, job_id: "job-id"}), {
      status: 202,
      headers: {"Content-Type": "application/json"},
    }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", {randomUUID: () => "11111111-1111-4111-8111-111111111111"});

    await new ApiClient("ua_test-token-value-that-is-long-enough").createJob({
      sourceURL: "https://u2.dmhy.org/details.php?id=60635",
      target: "MTEAM",
      executionMode: "step",
      downloaderName: "box",
      savePath: "/downloads",
			applyLabels: true,
      screenshotProfile: "default",
      imageHost: "default",
      tmdbProvider: "tmdb-main",
      ptgenProvider: "ptgen-main",
    });

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v2/jobs");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer ua_test-token-value-that-is-long-enough");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("11111111-1111-4111-8111-111111111111");
    const body = JSON.parse(String(init.body));
    expect(body.input.confirm_upload).toBe(false);
    expect(body.input.accept_rules).toBeUndefined();
    expect(body.execution_mode).toBe("step");
		expect(body.input.downloader).toMatchObject({apply_labels: true, category: "retorrent-source", tags: ["upload-assistant", "source"]});
		expect(body.input.target_downloader).toEqual({apply_labels: true});
		expect(body.input.metadata_providers).toEqual({tmdb: "tmdb-main", ptgen: "ptgen-main"});
  });

	it("requires an explicit no-label workflow control for capability-limited downloaders", async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ok: true, job_id: "job-id"}), {
			status: 202, headers: {"Content-Type": "application/json"},
		}));
		vi.stubGlobal("fetch", fetchMock);
		vi.stubGlobal("crypto", {randomUUID: () => "33333333-3333-4333-8333-333333333333"});
		await new ApiClient("ua_test-token-value-that-is-long-enough").createJob({
			sourceURL: "https://u2.dmhy.org/details.php?id=60635", target: "MTEAM", executionMode: "step",
			downloaderName: "deluge-box", savePath: "/downloads", applyLabels: false,
			screenshotProfile: "default", imageHost: "default",
		});
		const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
		expect(body.input.downloader).toEqual({name: "deluge-box", save_path: "/downloads", apply_labels: false, skip_checking: false, paused: false});
		expect(body.input.target_downloader).toEqual({apply_labels: false});
		expect(JSON.stringify(body.input)).not.toContain("category");
		expect(JSON.stringify(body.input)).not.toContain("tags");
	});

  it("creates audited candidate scans and submits candidates only in step mode", async () => {
	const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ok: true, job_id: "job-id"}), {
	  status: 202, headers: {"Content-Type": "application/json"},
	})));
	vi.stubGlobal("fetch", fetchMock);
	vi.stubGlobal("crypto", {randomUUID: () => "22222222-2222-4222-8222-222222222222"});
	const client = new ApiClient("ua_test-token-value-that-is-long-enough");

	await client.createDailyCandidateJob({source: "u2", target: "mteam", targetCount: 10, scanLimit: 30});
	await client.submitDailyCandidate("55555555-5555-4555-8555-555555555555");
	await client.createDailyCandidateSchedule({name: "morning", source: "u2", target: "mteam", cronExpression: "0 9 * * *", timezone: "Asia/Shanghai"});

	const [scanPath, scanInit] = fetchMock.mock.calls[0] as [string, RequestInit];
	expect(scanPath).toBe("/api/v2/candidates/daily");
	expect(JSON.parse(String(scanInit.body))).toMatchObject({source: "U2", target: "MTEAM", target_count: 10, execution_mode: "auto"});
	const [submitPath, submitInit] = fetchMock.mock.calls[1] as [string, RequestInit];
	expect(submitPath).toBe("/api/v2/candidates/55555555-5555-4555-8555-555555555555/retorrent-job");
	const submitBody = JSON.parse(String(submitInit.body));
	expect(submitBody.execution_mode).toBe("step");
	expect(submitBody.accept_rules).toBeUndefined();
	expect(submitBody.confirm_upload).toBeUndefined();
	const [schedulePath, scheduleInit] = fetchMock.mock.calls[2] as [string, RequestInit];
	expect(schedulePath).toBe("/api/v2/schedules/daily-candidates");
	const scheduleBody = JSON.parse(String(scheduleInit.body));
	expect(scheduleBody).toMatchObject({cron_expression: "0 9 * * *", timezone: "Asia/Shanghai", config: {source: "U2", target: "MTEAM", target_count: 10}});
	expect(scheduleBody.config.notification_channels).toEqual([]);
	expect(JSON.stringify(scheduleBody)).not.toContain("confirm_upload");
  });

  it("executes legacy migration only with the reviewed fingerprint and explicit confirmation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ok: true, status: "complete", import_id: "import-id"}), {
      status: 201, headers: {"Content-Type": "application/json"},
    }));
    vi.stubGlobal("fetch", fetchMock);
    const fingerprint = "a".repeat(64);
    await new ApiClient("ua_test-token-value-that-is-long-enough").executeLegacyMigration(fingerprint);

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v2/migrations/legacy");
    expect(JSON.parse(String(init.body))).toEqual({source_fingerprint: fingerprint, confirm_import: true});
    expect(String(init.body)).not.toContain("password");
  });

  it("lists global audit events with exact filters and an opaque cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true, status: "ready", audit_events: [], has_more: false, next_cursor: "", blockers: [], next_actions: [],
    }), {status: 200, headers: {"Content-Type": "application/json"}}));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("ua_test-token-value-that-is-long-enough").listAuditEvents({
      actorType: "worker", action: "downloader.torrent.add", resourceType: "downloader",
      resourceID: "box", limit: 20, cursor: "opaque",
    });
    const [path] = fetchMock.mock.calls[0] as [string, RequestInit];
    const query = new URLSearchParams(path.split("?")[1]);
    expect(path.split("?")[0]).toBe("/api/v2/audit-events");
    expect(Object.fromEntries(query)).toEqual({
      limit: "20", actor_type: "worker", action: "downloader.torrent.add",
      resource_type: "downloader", resource_id: "box", cursor: "opaque",
    });
  });

  it("lists redacted job attempts with an opaque cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true, status: "blocked", job_id: "job-id", attempts: [], has_more: false,
      next_cursor: "", blockers: [], next_actions: [],
    }), {status: 200, headers: {"Content-Type": "application/json"}}));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("ua_test-token-value-that-is-long-enough").getAttempts("job/id", "opaque-attempt-cursor");
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v2/jobs/job%2Fid/attempts?limit=500&cursor=opaque-attempt-cursor");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer ua_test-token-value-that-is-long-enough");
  });

  it("creates a fresh step-mode replay without consent fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true, status: "queued", job_id: "new-job", replay_of_job_id: "old-job",
    }), {status: 202, headers: {"Content-Type": "application/json"}}));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", {randomUUID: () => "44444444-4444-4444-8444-444444444444"});
    await new ApiClient("ua_test-token-value-that-is-long-enough").replayJob("old/job");
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v2/jobs/old%2Fjob/replay");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("44444444-4444-4444-8444-444444444444");
    expect(JSON.parse(String(init.body))).toEqual({execution_mode: "step"});
    expect(String(init.body)).not.toContain("confirm_upload");
    expect(String(init.body)).not.toContain("accept_rules");
  });

  it("requests a local-only live readiness report without confirmation fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: false, status: "blocked", configuration_ready: false,
      external_calls_performed: false, live_upload_authorized: false,
      source: "U2", target: "MTEAM", checks: [], required_confirmations: [], blockers: [], next_actions: [],
      resume_state: {accept_rules: {}, confirm_upload: false}, summary: "local only",
    }), {status: 200, headers: {"Content-Type": "application/json"}}));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("ua_test-token-value-that-is-long-enough").getLiveReadiness({
      source: "U2", target: "MTEAM", downloader: "box", targetDownloader: "seedbox",
		imageHost: "imgbb", screenshotProfile: "default",
		tmdbProvider: "tmdb-main", ptgenProvider: "ptgen-main",
    });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const query = new URLSearchParams(path.split("?")[1]);
    expect(path.split("?")[0]).toBe("/api/v2/readiness/live");
    expect(Object.fromEntries(query)).toEqual({
      source: "U2", target: "MTEAM", downloader: "box", target_downloader: "seedbox",
		image_host: "imgbb", screenshot_profile: "default", tmdb_provider: "tmdb-main", ptgen_provider: "ptgen-main",
    });
    expect(init.method).toBeUndefined();
    expect(path).not.toContain("confirm_upload");
  });

	it("queries lightweight operational logs with indexed filters and a cursor", async () => {
		const envelope={ok:true,status:"ready",operational_logs:[],has_more:true,next_cursor:"opaque-log-cursor",blockers:[],next_actions:[]};
		const fetchMock=vi.fn().mockResolvedValue(new Response(JSON.stringify(envelope),{status:200,headers:{"Content-Type":"application/json"}}));
		vi.stubGlobal("fetch",fetchMock);
		const result=await new ApiClient("ua_test-token-value-that-is-long-enough").listOperationalLogs({level:"error",component:"external.llm",query:"provider timeout",errorCode:"provider_timeout",statusCode:524,from:"2026-08-10T00:00:00Z",to:"2026-08-11T00:00:00Z",cursor:"opaque-log-cursor",limit:200});
		expect(result.next_cursor).toBe("opaque-log-cursor");
		const [path]=fetchMock.mock.calls[0] as [string,RequestInit];const query=new URLSearchParams(path.split("?")[1]);
		expect(Object.fromEntries(query)).toEqual({limit:"200",level:"error",q:"provider timeout",component:"external.llm",error_code:"provider_timeout",status_code:"524",from:"2026-08-10T00:00:00Z",to:"2026-08-11T00:00:00Z",cursor:"opaque-log-cursor"});
	});

	it("parses authenticated resumable operational-log SSE", async () => {
		const entry={id:43,occurred_at:"2026-08-10T07:15:00Z",level:"error",component:"external.llm",message:"failed",error_code:"provider_http_error"};
		const fetchMock=vi.fn().mockResolvedValue(new Response(`id: 43\nevent: operational-log\ndata: ${JSON.stringify(entry)}\n\n`,{status:200,headers:{"Content-Type":"text/event-stream"}}));
		vi.stubGlobal("fetch",fetchMock);const received:Array<typeof entry>=[];const controller=new AbortController();
		await new ApiClient("ua_test-token-value-that-is-long-enough").streamOperationalLogs({level:"error",afterID:42},value=>received.push(value as typeof entry),controller.signal);
		expect(received).toEqual([entry]);const [path,init]=fetchMock.mock.calls[0] as [string,RequestInit];
		expect(path).toContain("level=error");expect(new Headers(init.headers).get("Authorization")).toBe("Bearer ua_test-token-value-that-is-long-enough");expect(new Headers(init.headers).get("Last-Event-ID")).toBe("42");
	});

	it("recovers a proxy-interrupted rule analysis stream by polling the same idempotent request", async () => {
		vi.useFakeTimers();
		const analysis={draft_markdown:"draft",source_sha256:"a".repeat(64),provider_id:"provider",provider_name:"Provider",model:"model",reasoning_effort:"high",source_complete:true,confidence:.8,warnings:[],prompt_version:"site-rule-analysis-v2",external_call_performed:true};
		const interrupted=new ReadableStream<Uint8Array>({start(controller){controller.enqueue(new TextEncoder().encode('event: analysis-started\ndata: {"status":"analyzing"}\n\n'));controller.error(new TypeError("network error"))}});
		const fetchMock=vi.fn()
			.mockResolvedValueOnce(new Response(interrupted,{status:200,headers:{"Content-Type":"text/event-stream"}}))
			.mockResolvedValueOnce(new Response(JSON.stringify({ok:true,status:"analyzing"}),{status:202,headers:{"Content-Type":"application/json"}}))
			.mockResolvedValueOnce(new Response(JSON.stringify({ok:true,status:"draft_ready",analysis}),{status:200,headers:{"Content-Type":"application/json"}}));
		vi.stubGlobal("fetch",fetchMock);vi.stubGlobal("crypto",{randomUUID:()=>"77777777-7777-4777-8777-777777777777"});
		const resultPromise=new ApiClient("ua_test-token-value-that-is-long-enough").analyzeRuleText({providerID:"provider",sourceRevisionID:"revision"});
		await vi.runAllTimersAsync();
		expect(await resultPromise).toEqual(analysis);
		expect(fetchMock).toHaveBeenCalledTimes(3);
		const streamHeaders=new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);const pollHeaders=new Headers((fetchMock.mock.calls[1][1] as RequestInit).headers);
		expect(streamHeaders.get("Idempotency-Key")).toBe("77777777-7777-4777-8777-777777777777");expect(pollHeaders.get("Idempotency-Key")).toBe(streamHeaders.get("Idempotency-Key"));
		expect(fetchMock.mock.calls[1][0]).toBe("/api/v2/site-rules/analyze/result");
	});

	it("saves exact rule sources and observes the asynchronous collection stream", async () => {
		const fingerprint = "c".repeat(64);
		const run = {id:"88888888-8888-4888-8888-888888888888",site_code:"MTEAM",source_set_fingerprint:fingerprint,provider_id:"22222222-2222-4222-8222-222222222222",status:"ready",not_before:"2026-08-10T12:00:00Z",rule_revision_id:"99999999-9999-4999-8999-999999999999",documents:[],created_at:"2026-08-10T12:00:00Z",updated_at:"2026-08-10T12:01:00Z"};
		const sourceSet = {site_code:"MTEAM",sources:[{id:"titles",url:"https://wiki.m-team.cc/zh-tw/upload-title-rules",scope:"标题规范",auth_mode:"none" as const}],fingerprint,scope_confirmed:true,cookie_hosts_confirmed:false,cookie_configured:false,cookie_required:false};
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(new Response(JSON.stringify({source_set:sourceSet}),{status:200,headers:{"Content-Type":"application/json"}}))
			.mockResolvedValueOnce(new Response(JSON.stringify({run}),{status:202,headers:{"Content-Type":"application/json"}}))
			.mockResolvedValueOnce(new Response(`event: progress\ndata: ${JSON.stringify({run})}\n\n`,{status:200,headers:{"Content-Type":"text/event-stream"}}));
		vi.stubGlobal("fetch",fetchMock);vi.stubGlobal("crypto",{randomUUID:()=>"77777777-7777-4777-8777-777777777777"});
		const client = new ApiClient("ua_test-token-value-that-is-long-enough");
		const saved = await client.putRuleSourceSet("MTEAM",{sources:sourceSet.sources,scope_confirmed:true,cookie_hosts_confirmed:false});
		const created = await client.createRuleCollectionRun("MTEAM",saved.fingerprint,run.provider_id);
		const observed: Array<typeof run> = [];
		await client.streamRuleCollectionRun(created.id,(value)=>observed.push(value as typeof run),new AbortController().signal);
		expect(observed).toEqual([run]);
		expect(fetchMock.mock.calls[0][0]).toBe("/api/v2/sites/MTEAM/rule-sources");
		expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({sources:sourceSet.sources,scope_confirmed:true,cookie_hosts_confirmed:false});
		expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))).toEqual({source_set_fingerprint:fingerprint,provider_id:run.provider_id,confirm:true});
		expect(new Headers((fetchMock.mock.calls[1][1] as RequestInit).headers).get("Idempotency-Key")).toBe("77777777-7777-4777-8777-777777777777");
		expect(fetchMock.mock.calls[2][0]).toBe(`/api/v2/site-rule-collection-runs/${run.id}/stream`);
	});

	it("continues polling the same rule collection run when its SSE is interrupted", async () => {
		vi.useFakeTimers();
		const base = {id:"88888888-8888-4888-8888-888888888888",site_code:"CHD",source_set_fingerprint:"c".repeat(64),provider_id:"22222222-2222-4222-8222-222222222222",not_before:"2026-08-11T02:00:00Z",documents:[],created_at:"2026-08-11T02:00:00Z",updated_at:"2026-08-11T02:00:01Z"};
		const analyzing = {...base,status:"analyzing"};
		const ready = {...base,status:"ready",rule_revision_id:"99999999-9999-4999-8999-999999999999",completed_at:"2026-08-11T02:02:40Z"};
		const interrupted = new ReadableStream<Uint8Array>({start(controller){controller.enqueue(new TextEncoder().encode(`event: progress\ndata: ${JSON.stringify({run:analyzing})}\n\n`));controller.error(new TypeError("network error"))}});
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(new Response(interrupted,{status:200,headers:{"Content-Type":"text/event-stream"}}))
			.mockResolvedValueOnce(new Response(JSON.stringify({run:analyzing}),{status:200,headers:{"Content-Type":"application/json"}}))
			.mockResolvedValueOnce(new Response(JSON.stringify({run:ready}),{status:200,headers:{"Content-Type":"application/json"}}));
		vi.stubGlobal("fetch",fetchMock);
		const observed:string[]=[];
		const resultPromise=new ApiClient("ua_test-token-value-that-is-long-enough").streamRuleCollectionRun(base.id,run=>observed.push(run.status),new AbortController().signal);
		await vi.runAllTimersAsync();
		await resultPromise;
		expect(observed).toEqual(["analyzing","ready"]);
		expect(fetchMock).toHaveBeenCalledTimes(3);
		expect(fetchMock.mock.calls.slice(1).every(([path])=>path===`/api/v2/site-rule-collection-runs/${base.id}`)).toBe(true);
	});
});
