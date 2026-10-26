export type Parameter = {
  kind: string;
  allowed_values: string[];
  required: boolean;
  default: string | null;
};
export type Project = {
  provider: string;
  framework: string | null;
  repository: string;
  default_branch: string;
  allowed_branches: string[];
  enabled: boolean;
  pipelines: Record<
    string,
    { sut_ids?: string[]; selector: string; parameters: Record<string, Parameter> }
  >;
  reports: { format: string; path: string }[];
};
export type Registry = { projects: Record<string, Project> };
export type Plan = {
  expired_unclaimed?: boolean;
  id: string;
  commit: string;
  selector: string;
  status: string;
  expires_at: number;
  error: string | null;
  pipeline: { uuid: string; url: string } | null;
  plan: {
    sut_id?: string | null;
    project_id: string;
    pipeline_id: string;
    branch: string;
    parameters: Record<string, string>;
    project: Project;
  };
};
export type Run = {
  sut_id?: string | null;
  id: string;
  project_id: string;
  pipeline: { uuid: string; url: string };
  pipeline_status: string;
  test_status: string;
  terminal: boolean;
  monitoring_status: string;
  last_checked_at: number | null;
  error: { code: string; message: string } | null;
  cancel_status: string;
  cancel_error: { code: string; message: string } | null;
  steps: { id: string; name: string; status: string; result: string | null }[];
};
export type Evidence = {
  run_id: string;
  test_status: string;
  analysis_ready: boolean;
  evidence_complete: boolean;
  evidence_limitations: string[];
  artifacts: {
    id?: string;
    name: string;
    format: string;
    availability: string;
    parse_status: string;
    source?: string;
    association?: string;
    error?: string | null;
  }[];
  tests: {
    id: string;
    name: string;
    status: string;
    message: string;
    artifact_ids: string[];
    attempts: number | null;
  }[];
};
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 75000);
  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      cache: "no-store",
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((x: { msg: string }) => x.msg).join("; ")
            : detail?.message || `Request failed (HTTP ${response.status})`;
      throw new ApiError(message, response.status, typeof detail?.code === "string" ? detail.code : undefined);
    }
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new Error(
      "Backend unavailable or request timed out. Check the connection; execution may still have been accepted.",
    );
  } finally {
    clearTimeout(timeout);
  }
}
export function post<T>(path: string, body?: unknown) {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
export function safeBitbucketUrl(
  value: string | undefined,
): string | undefined {
  try {
    const u = new URL(value || "");
    return u.protocol === "https:" &&
      u.hostname === "bitbucket.org" &&
      !u.username &&
      !u.password
      ? u.href
      : undefined;
  } catch {
    return undefined;
  }
}
