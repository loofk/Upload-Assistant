import {cleanup, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, describe, expect, it, vi} from "vitest";
import {ApiClient} from "./api";
import {ImageHostsPanel, MetadataProvidersPanel, NotificationChannelsPanel} from "./Configuration";

describe("ImageHostsPanel", () => {
	afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

	it("offers Imgbox and Pixhost without showing an API key input", async () => {
		const client = {putImageHost: vi.fn()} as unknown as ApiClient;
		render(<ImageHostsPanel items={[]} client={client} reload={vi.fn()} onError={vi.fn()} />);
		await userEvent.click(screen.getByRole("button", {name: "新增图床"}));
		const selector = screen.getByLabelText("图床服务");
		expect(withOptionLabels(selector)).toEqual(expect.arrayContaining(["Imgbox（无需 Key）", "Pixhost（无需 Key）"]));
		expect(screen.getByLabelText("API Key（留空则保留）")).toBeInTheDocument();
		await userEvent.selectOptions(selector, "pixhost");
		expect(screen.queryByLabelText("API Key（留空则保留）")).not.toBeInTheDocument();
		expect(screen.getByText("该图床无需凭据，保存后即可供任务选择。")).toBeInTheDocument();
		expect(screen.queryByRole("button", {name: "凭据保存说明"})).not.toBeInTheDocument();
	});

	it("requires confirmation before uploading the image-host probe", async () => {
		const probeImageHost = vi.fn().mockResolvedValue({ok: true, status: "ready"});
		const reload = vi.fn().mockResolvedValue(undefined);
		const confirm = vi.fn().mockReturnValue(true);
		vi.stubGlobal("confirm", confirm);
		const client = {probeImageHost} as unknown as ApiClient;
		render(<ImageHostsPanel items={[{
			id: "image-id", name: "pixhost-main", adapter: "pixhost", enabled: true, priority: 100,
			config: {endpoint: "https://api.pixhost.to/images"}, credential_fields: [], health_status: "unknown",
			created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
		}]} client={client} reload={reload} onError={vi.fn()} />);
		await userEvent.click(screen.getByRole("button", {name: "测试图床"}));
		expect(confirm).toHaveBeenCalledWith(expect.stringContaining("上传一张 100×100 测试图"));
		await waitFor(() => expect(probeImageHost).toHaveBeenCalledWith("pixhost-main"));
		expect(await screen.findByText("测试图上传成功，连接状态已更新。")).toBeInTheDocument();
	});

	it("offers explicit notification delivery and metadata query tests", async () => {
		const probeNotificationChannel = vi.fn().mockResolvedValue({ok: true, status: "sent"});
		const probeMetadataProvider = vi.fn().mockResolvedValue({ok: true, status: "ready"});
		vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
		const client = {probeNotificationChannel, probeMetadataProvider} as unknown as ApiClient;
		const {unmount} = render(<NotificationChannelsPanel items={[{
			id: "notice-id", name: "alerts", adapter: "telegram_bot", enabled: true,
			config: {event_types: ["step.failed"]}, credential_fields: ["bot_token", "chat_id"], health_status: "unknown",
			created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
		}]} client={client} reload={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
		await userEvent.click(screen.getByRole("button", {name: "发送测试消息"}));
		await waitFor(() => expect(probeNotificationChannel).toHaveBeenCalledWith("alerts"));
		unmount();

		render(<MetadataProvidersPanel items={[{
			id: "metadata-id", name: "tmdb-main", adapter: "tmdb", enabled: true,
			config: {endpoint: "https://api.themoviedb.org"}, credential_fields: ["api_key"], health_status: "unknown",
			created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
		}]} client={client} reload={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
		await userEvent.click(screen.getByRole("button", {name: "测试查询"}));
		await waitFor(() => expect(probeMetadataProvider).toHaveBeenCalledWith("tmdb-main"));
	});
});

function withOptionLabels(element: HTMLElement): string[] {
	return Array.from((element as HTMLSelectElement).options).map((option) => option.textContent ?? "");
}
