export interface Env {
  CONTROL_BUCKET: R2Bucket;
  MEDLEARN_INGEST_TOKEN: string;
  GITHUB_ACTIONS_DISPATCH_TOKEN: string;
}

type Status = "received" | "dispatched" | "running" | "succeeded" | "blocked" | "failed" | "expired";

export interface JobRecord {
  job_version: "0.1.0";
  job_id: string;
  status: Status;
  draft_digest: string;
  draft_object_key: string;
  proposal_id?: string;
  workflow_run_id?: string;
  created_at: string;
  updated_at: string;
  error_code?: string;
}

const MAX_BODY = 1024 * 1024;
const CURRENT_DRAFT_VERSION = "0.3.0";
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const PROPOSAL_ID = /^proposal_[a-f0-9]{32}$/;
const jsonHeaders = { "content-type": "application/json; charset=utf-8" };

function reply(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: jsonHeaders });
}

async function sha256(value: ArrayBuffer | string): Promise<string> {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function authorized(request: Request, secret: string): Promise<boolean> {
  const supplied = request.headers.get("authorization") ?? "";
  const expected = `Bearer ${secret}`;
  const [a, b] = await Promise.all([sha256(supplied), sha256(expected)]);
  return a === b;
}

async function readJson<T>(bucket: R2Bucket, key: string): Promise<T | null> {
  const object = await bucket.get(key);
  return object ? object.json<T>() : null;
}

function sanitizeJob(job: JobRecord): JobRecord {
  const clean: JobRecord = {
    job_version: job.job_version,
    job_id: job.job_id,
    status: job.status,
    draft_digest: job.draft_digest,
    draft_object_key: job.draft_object_key,
    created_at: job.created_at,
    updated_at: job.updated_at,
  };
  if (job.proposal_id !== undefined) clean.proposal_id = job.proposal_id;
  if (job.workflow_run_id !== undefined) clean.workflow_run_id = job.workflow_run_id;
  if (job.error_code !== undefined) clean.error_code = job.error_code;
  return clean;
}

async function putNew(bucket: R2Bucket, key: string, value: string | ArrayBuffer): Promise<boolean> {
  return (await bucket.put(key, value, { onlyIf: { etagDoesNotMatch: "*" } })) !== null;
}

function validEnvelope(value: unknown): value is { client_kind: string; draft: { draft_version: string } } {
  if (!value || typeof value !== "object") return false;
  const envelope = value as Record<string, unknown>;
  if (!(["chatgpt_work", "ios_shortcut", "manual"] as unknown[]).includes(envelope.client_kind)) return false;
  if (!envelope.draft || typeof envelope.draft !== "object") return false;
  return typeof (envelope.draft as Record<string, unknown>).draft_version === "string";
}

async function dispatch(env: Env, job: JobRecord): Promise<boolean> {
  try {
    const response = await fetch(
      "https://api.github.com/repos/fangzh05/medlearn-vault/actions/workflows/medlearn-propose.yml/dispatches",
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.GITHUB_ACTIONS_DISPATCH_TOKEN}`,
          accept: "application/vnd.github+json",
          "content-type": "application/json",
          "user-agent": "medlearn-cloud",
          "x-github-api-version": "2022-11-28",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: {
            job_id: job.job_id,
            draft_object_key: job.draft_object_key,
            draft_digest: job.draft_digest,
          },
        }),
      },
    );
    return response.ok;
  } catch {
    return false;
  }
}

async function createCapture(request: Request, env: Env): Promise<Response> {
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json")
    return reply(415, { error: "INVALID_CONTENT_TYPE" });
  const idempotencyKey = request.headers.get("idempotency-key");
  if (!idempotencyKey || idempotencyKey.length > 512) return reply(400, { error: "INVALID_IDEMPOTENCY_KEY" });
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_BODY) return reply(413, { error: "BODY_TOO_LARGE" });
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return reply(413, { error: "BODY_TOO_LARGE" });
  let parsed: unknown;
  try { parsed = JSON.parse(new TextDecoder().decode(body)); }
  catch { return reply(400, { error: "INVALID_JSON" }); }
  if (!validEnvelope(parsed)) return reply(400, { error: "INVALID_ENVELOPE" });
  if (parsed.draft.draft_version !== CURRENT_DRAFT_VERSION) return reply(422, { error: "UNSUPPORTED_DRAFT_VERSION" });

  const digest = await sha256(body);
  const draftDigest = `sha256:${digest}`;
  const draftKey = `v1/drafts/sha256/${digest}.json`;
  const idemKey = `v1/idempotency/${await sha256(idempotencyKey)}.json`;
  const jobId = crypto.randomUUID();
  const now = new Date().toISOString();
  const job: JobRecord = {
    job_version: "0.1.0", job_id: jobId, status: "received", draft_digest: draftDigest,
    draft_object_key: draftKey, created_at: now, updated_at: now,
  };
  const claim = JSON.stringify({ job_id: jobId, draft_digest: draftDigest, job });
  if (!(await putNew(env.CONTROL_BUCKET, idemKey, claim))) {
    const existing = await readJson<{ job_id: string; draft_digest: string; job: JobRecord }>(env.CONTROL_BUCKET, idemKey);
    if (!existing) return reply(503, { error: "IDEMPOTENCY_UNAVAILABLE" });
    if (existing.draft_digest !== draftDigest) return reply(409, { error: "IDEMPOTENCY_CONFLICT" });
    const original = await readJson<JobRecord>(env.CONTROL_BUCKET, `v1/jobs/${existing.job_id}.json`);
    return reply(202, sanitizeJob(original ?? existing.job));
  }

  await putNew(env.CONTROL_BUCKET, draftKey, body);
  await env.CONTROL_BUCKET.put(`v1/jobs/${jobId}.json`, JSON.stringify(job));
  if (await dispatch(env, job)) {
    const dispatched = { ...job, status: "dispatched" as const, updated_at: new Date().toISOString() };
    await env.CONTROL_BUCKET.put(`v1/jobs/${jobId}.json`, JSON.stringify(dispatched));
    return reply(202, dispatched);
  }
  const failed = { ...job, status: "failed" as const, updated_at: new Date().toISOString(), error_code: "GITHUB_DISPATCH_FAILED" };
  await env.CONTROL_BUCKET.put(`v1/jobs/${jobId}.json`, JSON.stringify(failed));
  return reply(502, failed);
}

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/") return reply(200, { service: "medlearn-cloud", status: "ok" });
  if (request.method === "GET" && url.pathname === "/health") return reply(200, { status: "ok" });
  if (url.pathname.startsWith("/v1/") && !(await authorized(request, env.MEDLEARN_INGEST_TOKEN)))
    return reply(401, { error: "UNAUTHORIZED" });
  if (request.method === "POST" && url.pathname === "/v1/captures") return createCapture(request, env);
  const jobMatch = request.method === "GET" && url.pathname.match(/^\/v1\/jobs\/([^/]+)$/);
  if (jobMatch) {
    if (!ID.test(jobMatch[1])) return reply(400, { error: "INVALID_IDENTIFIER" });
    const job = await readJson<JobRecord>(env.CONTROL_BUCKET, `v1/jobs/${jobMatch[1]}.json`);
    return job ? reply(200, sanitizeJob(job)) : reply(404, { error: "NOT_FOUND" });
  }
  const proposalMatch = request.method === "GET" && url.pathname.match(/^\/v1\/proposals\/([^/]+)$/);
  if (proposalMatch) {
    if (!PROPOSAL_ID.test(proposalMatch[1])) return reply(400, { error: "INVALID_IDENTIFIER" });
    const proposal = await readJson<unknown>(env.CONTROL_BUCKET, `v1/proposals/${proposalMatch[1]}.json`);
    return proposal ? reply(200, proposal) : reply(404, { error: "NOT_FOUND" });
  }
  return reply(404, { error: "NOT_FOUND" });
}

export default { fetch: handle };
