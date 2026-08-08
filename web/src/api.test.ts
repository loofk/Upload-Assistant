import {afterEach, describe, expect, it, vi} from "vitest";
import {ApiClient} from "./api";

describe("ApiClient safety defaults", () => {
  afterEach(() => vi.unstubAllGlobals());

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
});
