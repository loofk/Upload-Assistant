import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import App from "./App";

describe("App authentication boundary", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

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
});
