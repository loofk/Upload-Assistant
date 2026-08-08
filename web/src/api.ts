import type {
  CreateJobInput,
  EventsEnvelope,
  JobEnvelope,
  JobListEnvelope,
  JobStatus,
  JobSummaryEnvelope,
  JsonValue,
} from "./types";

interface ProblemBody {
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

export class ApiClient {
  constructor(private readonly token: string) {}

  async listJobs(options: {status?: JobStatus | ""; cursor?: string; limit?: number} = {}): Promise<JobListEnvelope> {
    const query = new URLSearchParams({kind: "retorrent", limit: String(options.limit ?? 25)});
    if (options.status) query.set("status", options.status);
    if (options.cursor) query.set("cursor", options.cursor);
    return this.request(`/api/v2/jobs?${query.toString()}`);
  }

  async getSummary(jobID: string): Promise<JobSummaryEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/summary`);
  }

  async getEvents(jobID: string): Promise<EventsEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/events?after=0&limit=500`);
  }

  async createJob(input: CreateJobInput): Promise<JobEnvelope> {
    const workflowInput: Record<string, JsonValue> = {
      source_url: input.sourceURL,
      target: input.target.toUpperCase(),
      confirm_upload: false,
      downloader: {
        name: input.downloaderName,
        save_path: input.savePath,
        category: "retorrent-source",
        tags: ["upload-assistant", "source"],
        skip_checking: false,
        paused: false,
      },
      screenshots: {profile: input.screenshotProfile},
      image_host: {name: input.imageHost},
    };
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

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {...init, headers, credentials: "same-origin"});
    if (!response.ok) {
      let problem: ProblemBody = {};
      try {
        problem = (await response.json()) as ProblemBody;
      } catch {
        // Keep a bounded generic error when a proxy returns non-JSON.
      }
      const code = problem.error?.code ?? problem.blockers?.[0]?.code ?? "request_failed";
      const detail = problem.error?.detail ?? problem.blockers?.[0]?.message ?? `请求失败（HTTP ${response.status}）`;
      throw new ApiError(response.status, code, detail);
    }
    return (await response.json()) as T;
  }
}
