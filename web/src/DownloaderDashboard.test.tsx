import {cleanup, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, describe, expect, it, vi} from "vitest";
import type {ApiClient} from "./api";
import DownloaderDashboard from "./DownloaderDashboard";

describe("DownloaderDashboard", () => {
	afterEach(cleanup);

	it("shows a read-only transfer snapshot and opens bounded file details", async () => {
		const torrent = {
			hash: "a".repeat(40), name: "Example.Release.2026.1080p", state: "uploading", state_group: "seeding" as const,
			progress: 1, total_size: 2 * 1024 ** 3, amount_left: 0, downloaded: 2 * 1024 ** 3, uploaded: 4 * 1024 ** 3,
			download_speed: 0, upload_speed: 2 * 1024 ** 2, download_limit: 0, upload_limit: 20 * 1024 ** 2, limits_available: true,
			ratio: 2, category: "MTEAM", tags: "retorrent", added_on: 100, completion_on: 110, time_active: 3600, seeding_time: 1800,
		};
		const client = {
			listDownloaders: vi.fn().mockResolvedValue([{
				id: "downloader-1", name: "box", adapter: "qbittorrent", enabled: true, network_class: "seedbox",
				adapter_capability: {display_name: "qBittorrent", operations: {list_torrents: true}},
			}]),
			getDownloaderSnapshot: vi.fn().mockResolvedValue({
				downloader_name: "box", adapter: "qbittorrent", network_class: "seedbox", fetched_at: "2026-08-11T08:00:00Z",
				summary: {total: 1, downloading: 0, seeding: 1, paused: 0, checking: 0, errors: 0, active: 1, download_speed: 0, upload_speed: 2 * 1024 ** 2},
				torrents: [torrent], filtered_total: 1, offset: 0, limit: 100, has_more: false,
			}),
			getDownloaderTorrentFiles: vi.fn().mockResolvedValue({files: [{index: 0, name: "Example/video.mkv", size: 2 * 1024 ** 3, progress: 1, priority: 1, is_seed: true, availability: 1}], file_count: 1, total_size: 2 * 1024 ** 3}),
		} as unknown as ApiClient;

		render(<DownloaderDashboard client={client} onError={vi.fn()} onOpenConfiguration={vi.fn()} />);
		expect(await screen.findByText("Example.Release.2026.1080p")).toBeInTheDocument();
		expect(screen.getByText("↑ 2.00 MiB/s")).toBeInTheDocument();
		expect(screen.getByText("MTEAM · retorrent")).toBeInTheDocument();

		await userEvent.click(screen.getByText("Example.Release.2026.1080p"));
		expect(await screen.findByRole("dialog", {name: "Example.Release.2026.1080p"})).toBeInTheDocument();
		expect(await screen.findByText("Example/video.mkv")).toBeInTheDocument();
		expect(screen.getByText("20.0 MiB/s")).toBeInTheDocument();

		await userEvent.click(screen.getByRole("button", {name: /有流量/}));
		await waitFor(() => expect(client.getDownloaderSnapshot).toHaveBeenCalledWith("box", expect.objectContaining({filter: "active"})));
	});
});
