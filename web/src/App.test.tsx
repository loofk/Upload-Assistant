import {cleanup, render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import App from "./App";

describe("App authentication boundary", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps the API token entry local to the current session", async () => {
    render(<App />);
    expect(screen.getByRole("heading", {name: "转种工作台"})).toBeInTheDocument();
    const token = screen.getByLabelText("API Token");
    expect(token).toHaveAttribute("type", "password");
    await userEvent.type(token, "ua_test-token-value-that-is-long-enough");
    expect(localStorage.getItem("ua.v2.api-token")).toBeNull();
  });

  it("loads independent integration management through authenticated APIs", async () => {
    sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
        : path === "/api/v2/downloaders" ? {downloaders: []}
        : path === "/api/v2/image-hosts" ? {image_hosts: []}
        : path === "/api/v2/screenshot-profiles" ? {screenshot_profiles: []}
        : path === "/api/v2/sites" ? {sites: []}
        : {};
      return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await userEvent.click(screen.getByRole("button", {name: "配置"}));
    expect(await screen.findByRole("heading", {name: "配置中心"})).toBeInTheDocument();
    expect(await screen.findByText("尚未配置下载器。")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v2/downloaders", expect.objectContaining({credentials: "same-origin"}));
    const downloaderCall = fetchMock.mock.calls.find(([path]) => path === "/api/v2/downloaders");
    expect(new Headers(downloaderCall?.[1]?.headers).get("Authorization")).toBe("Bearer ua_test-token-value-that-is-long-enough");
  });

  it("shows ranked daily candidates and their safety evidence", async () => {
	sessionStorage.setItem("ua.v2.api-token", "ua_test-token-value-that-is-long-enough");
	const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
	  const path = String(input);
	  const payload = path.startsWith("/api/v2/jobs?") ? {ok: true, status: "ready", jobs: [], has_more: false, next_cursor: ""}
		: path.startsWith("/api/v2/candidates/daily?") ? {ok: true, status: "ready", date: "2026-08-08", count: 1, ready_count: 1, blockers: [], next_actions: [], candidates: [{
		  id: "55555555-5555-4555-8555-555555555555", source_site: "U2", target_site: "MTEAM", source_torrent_id: "60635",
		  recommendation_date: "2026-08-08T00:00:00Z", rank: 1, score: 80, status: "candidate",
		  discovered_at: "2026-08-08T00:00:00Z", expires_at: "2026-08-10T00:00:00Z", updated_at: "2026-08-08T00:00:00Z",
		  payload: {ready: true, source: {title: "Fixture Anime", free: true, size_bytes: 1024}, metadata: {imdb_id: "tt1234567"}, duplicate_check: {duplicate: false}, recommendation_reasons: ["target_duplicate_clear"], risks: [], blockers: []},
		}]}
		: path.startsWith("/api/v2/schedules/daily-candidates?") ? {ok: true, status: "ready", count: 0, schedules: [], blockers: [], next_actions: []}
		: path.startsWith("/api/v2/notifications?") ? {ok: true, status: "ready", count: 0, notifications: [], blockers: [], next_actions: []}
		: {};
	  return Promise.resolve(new Response(JSON.stringify(payload), {status: 200, headers: {"Content-Type": "application/json"}}));
	});
	vi.stubGlobal("fetch", fetchMock);
	render(<App />);
	await userEvent.click(screen.getByRole("button", {name: "每日候选"}));
	expect(await screen.findByRole("heading", {name: "每日候选"})).toBeInTheDocument();
	expect(await screen.findByRole("heading", {name: "Fixture Anime"})).toBeInTheDocument();
	expect(screen.getByText("目标查重通过")).toBeInTheDocument();
  });
});
