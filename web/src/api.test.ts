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
  });
});
