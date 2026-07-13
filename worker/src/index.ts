import validateEnvelope from "./generated/intake-validator.js";

export interface Env {
  CONTROL_BUCKET?: R2Bucket;
  VAULT_BUCKET?: R2Bucket;
  MEDLEARN_INGEST_TOKEN?: string;
  MEDLEARN_SYNC_TOKEN?: string;
  GITHUB_ACTIONS_DISPATCH_TOKEN?: string;
}

interface Config {
  CONTROL_BUCKET: R2Bucket;
  MEDLEARN_INGEST_TOKEN: string;
  GITHUB_ACTIONS_DISPATCH_TOKEN: string;
}

interface VaultConfig {
  VAULT_BUCKET: R2Bucket;
  MEDLEARN_SYNC_TOKEN: string;
}

type Status = "received" | "dispatched" | "running" | "succeeded" | "blocked" | "failed" | "expired";

export interface JobRecord {
  job_version: "0.2.0";
  job_id: string;
  status: Status;
  intake_digest: string;
  intake_object_key: string;
  proposal_id?: string;
  workflow_run_id?: string;
  dispatch_attempt: number;
  dispatch_lease_id?: string;
  dispatch_lease_expires_at?: string;
  created_at: string;
  updated_at: string;
  error_code?: string;
}

interface IdempotencyRecord {
  idempotency_version: "0.1.0";
  job_id: string;
  intake_digest: string;
  created_at: string;
}

export interface Stored<T> { value: T; etag: string }

const MAX_BODY = 1024 * 1024;
const LEASE_MS = 30_000;
const CURRENT_INTAKE_VERSION = "0.1.0";
const CURRENT_DRAFT_VERSION = "0.3.0";
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const PROPOSAL_ID = /^proposal_[a-f0-9]{32}$/;

function reply(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function secure(response: Response): Response {
  response.headers.set("cache-control", "no-store");
  response.headers.set("vary", "Authorization");
  return response;
}

function config(env: Env): Config | null {
  const token = env.MEDLEARN_INGEST_TOKEN;
  const github = env.GITHUB_ACTIONS_DISPATCH_TOKEN;
  if (!env.CONTROL_BUCKET || typeof env.CONTROL_BUCKET.get !== "function") return null;
  if (typeof token !== "string" || token.length < 32) return null;
  if (typeof github !== "string" || github.trim().length === 0) return null;
  return { CONTROL_BUCKET: env.CONTROL_BUCKET, MEDLEARN_INGEST_TOKEN: token, GITHUB_ACTIONS_DISPATCH_TOKEN: github };
}

function vaultConfig(env: Env): VaultConfig | null {
  const token = env.MEDLEARN_SYNC_TOKEN;
  if (!env.VAULT_BUCKET || typeof env.VAULT_BUCKET.get !== "function") return null;
  if (typeof token !== "string" || token.length < 32) return null;
  return { VAULT_BUCKET: env.VAULT_BUCKET, MEDLEARN_SYNC_TOKEN: token };
}

async function sha256(value: ArrayBuffer | Uint8Array | string): Promise<string> {
  const bytes: Uint8Array = typeof value === "string"
    ? new TextEncoder().encode(value)
    : value instanceof Uint8Array ? value : new Uint8Array(value);
  // Get a fresh ArrayBuffer copy for type compatibility
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", buf as ArrayBuffer))]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function authorized(request: Request, secret: string): Promise<boolean> {
  const supplied = request.headers.get("authorization") ?? "";
  const [a, b] = await Promise.all([sha256(supplied), sha256(`Bearer ${secret}`)]);
  return a === b;
}

async function readStored<T>(bucket: R2Bucket, key: string): Promise<Stored<T> | null> {
  const object = await bucket.get(key);
  return object ? { value: await object.json<T>(), etag: object.etag } : null;
}

async function putNew(bucket: R2Bucket, key: string, value: string | ArrayBuffer): Promise<boolean> {
  return (await bucket.put(key, value, { onlyIf: new Headers({ "If-None-Match": "*" }) })) !== null;
}

async function putCas(bucket: R2Bucket, key: string, value: unknown, etag: string): Promise<boolean> {
  return (await bucket.put(key, JSON.stringify(value), { onlyIf: { etagMatches: etag } })) !== null;
}

const allowed: Record<Status, readonly Status[]> = {
  received: ["dispatched", "failed", "expired"],
  dispatched: ["running", "failed", "expired"],
  running: ["succeeded", "blocked", "failed", "expired"],
  failed: ["received", "expired"],
  succeeded: [],
  blocked: [],
  expired: [],
};

export function allowedJobTransition(from: Status, to: Status): boolean {
  return allowed[from].includes(to);
}

export async function transitionJob(
  bucket: R2Bucket, key: string, current: Stored<JobRecord>, next: JobRecord,
): Promise<boolean> {
  if (!allowedJobTransition(current.value.status, next.status)) return false;
  return putCas(bucket, key, next, current.etag);
}

function sanitizeJob(job: JobRecord): JobRecord {
  const clean: JobRecord = {
    job_version: "0.2.0", job_id: job.job_id, status: job.status,
    intake_digest: job.intake_digest, intake_object_key: job.intake_object_key,
    dispatch_attempt: job.dispatch_attempt, created_at: job.created_at, updated_at: job.updated_at,
  };
  if (job.proposal_id !== undefined) clean.proposal_id = job.proposal_id;
  if (job.workflow_run_id !== undefined) clean.workflow_run_id = job.workflow_run_id;
  if (job.dispatch_lease_id !== undefined) clean.dispatch_lease_id = job.dispatch_lease_id;
  if (job.dispatch_lease_expires_at !== undefined) clean.dispatch_lease_expires_at = job.dispatch_lease_expires_at;
  if (job.error_code !== undefined) clean.error_code = job.error_code;
  return clean;
}

async function dispatch(env: Config, job: JobRecord): Promise<boolean> {
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
            intake_object_key: job.intake_object_key,
            intake_digest: job.intake_digest,
          },
        }),
      },
    );
    return response.ok;
  } catch {
    return false;
  }
}

async function claimIdempotency(
  bucket: R2Bucket, key: string, intakeDigest: string, now: string,
): Promise<IdempotencyRecord> {
  const proposed: IdempotencyRecord = {
    idempotency_version: "0.1.0", job_id: crypto.randomUUID(), intake_digest: intakeDigest,
    created_at: now,
  };
  await putNew(bucket, key, JSON.stringify(proposed));
  const stored = await readStored<IdempotencyRecord>(bucket, key);
  if (!stored) throw new Error("IDEMPOTENCY_UNAVAILABLE");
  return stored.value;
}

async function ensureArtifacts(
  bucket: R2Bucket, claim: IdempotencyRecord, intakeKey: string, exactBody: ArrayBuffer, now: string,
): Promise<Stored<JobRecord>> {
  await putNew(bucket, intakeKey, exactBody);
  const jobKey = `v1/jobs/${claim.job_id}.json`;
  const initial: JobRecord = {
    job_version: "0.2.0", job_id: claim.job_id, status: "received",
    intake_digest: claim.intake_digest, intake_object_key: intakeKey, dispatch_attempt: 0,
    created_at: claim.created_at, updated_at: now,
  };
  await putNew(bucket, jobKey, JSON.stringify(initial));
  const job = await readStored<JobRecord>(bucket, jobKey);
  if (!job) throw new Error("JOB_UNAVAILABLE");
  return job;
}

async function dispatchRecoverably(env: Config, stored: Stored<JobRecord>, nowMs: number): Promise<Response> {
  const jobKey = `v1/jobs/${stored.value.job_id}.json`;
  const job = stored.value;
  if (["dispatched", "running", "succeeded", "blocked", "expired"].includes(job.status))
    return reply(202, sanitizeJob(job));
  const leaseExpiry = job.dispatch_lease_expires_at ? Date.parse(job.dispatch_lease_expires_at) : 0;
  if (job.dispatch_lease_id && leaseExpiry > nowMs) return reply(202, sanitizeJob(job));

  const leaseId = crypto.randomUUID();
  const leased: JobRecord = {
    ...job,
    status: job.status === "failed" ? "received" : job.status,
    dispatch_attempt: job.dispatch_attempt + 1,
    dispatch_lease_id: leaseId,
    dispatch_lease_expires_at: new Date(nowMs + LEASE_MS).toISOString(),
    updated_at: new Date(nowMs).toISOString(),
    error_code: undefined,
  };
  if (!(await putCas(env.CONTROL_BUCKET, jobKey, leased, stored.etag))) {
    const winner = await readStored<JobRecord>(env.CONTROL_BUCKET, jobKey);
    return winner ? reply(202, sanitizeJob(winner.value)) : reply(503, { error: "CONTROL_STORAGE_UNAVAILABLE" });
  }
  const leasedStored = await readStored<JobRecord>(env.CONTROL_BUCKET, jobKey);
  if (!leasedStored || leasedStored.value.dispatch_lease_id !== leaseId)
    return reply(503, { error: "CONTROL_STORAGE_UNAVAILABLE" });

  if (await dispatch(env, leased)) {
    const dispatched: JobRecord = {
      ...leased, status: "dispatched", updated_at: new Date().toISOString(),
      dispatch_lease_id: undefined, dispatch_lease_expires_at: undefined,
    };
    await transitionJob(env.CONTROL_BUCKET, jobKey, leasedStored, dispatched);
    const final = await readStored<JobRecord>(env.CONTROL_BUCKET, jobKey);
    return reply(202, sanitizeJob(final?.value ?? dispatched));
  }
  const failed: JobRecord = {
    ...leased, status: "failed", updated_at: new Date().toISOString(),
    dispatch_lease_id: undefined, dispatch_lease_expires_at: undefined,
    error_code: "GITHUB_DISPATCH_FAILED",
  };
  await transitionJob(env.CONTROL_BUCKET, jobKey, leasedStored, failed);
  const final = await readStored<JobRecord>(env.CONTROL_BUCKET, jobKey);
  return reply(502, sanitizeJob(final?.value ?? failed));
}

async function createCapture(request: Request, env: Config): Promise<Response> {
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
  const record = parsed as Record<string, unknown>;
  if (record?.intake_version !== CURRENT_INTAKE_VERSION)
    return reply(422, { error: "UNSUPPORTED_INTAKE_VERSION" });
  const draft = record?.draft as Record<string, unknown> | undefined;
  if (draft?.draft_version !== CURRENT_DRAFT_VERSION)
    return reply(422, { error: "UNSUPPORTED_DRAFT_VERSION" });
  if (!validateEnvelope(parsed)) return reply(400, { error: "INVALID_INTAKE_ENVELOPE" });

  const hex = await sha256(body);
  const intakeDigest = `sha256:${hex}`;
  const intakeKey = `v1/intakes/sha256/${hex}.json`;
  const idemKey = `v1/idempotency/${await sha256(idempotencyKey)}.json`;
  const nowMs = Date.now();
  const claim = await claimIdempotency(env.CONTROL_BUCKET, idemKey, intakeDigest, new Date(nowMs).toISOString());
  if (claim.intake_digest !== intakeDigest) return reply(409, { error: "IDEMPOTENCY_CONFLICT" });
  const job = await ensureArtifacts(env.CONTROL_BUCKET, claim, intakeKey, body, new Date(nowMs).toISOString());
  return dispatchRecoverably(env, job, nowMs);
}

async function routeV1(request: Request, env: Config, url: URL): Promise<Response> {
  if (!(await authorized(request, env.MEDLEARN_INGEST_TOKEN))) return reply(401, { error: "UNAUTHORIZED" });
  if (request.method === "POST" && url.pathname === "/v1/captures") return createCapture(request, env);
  const jobMatch = request.method === "GET" && url.pathname.match(/^\/v1\/jobs\/([^/]+)$/);
  if (jobMatch) {
    if (!ID.test(jobMatch[1])) return reply(400, { error: "INVALID_IDENTIFIER" });
    const job = await readStored<JobRecord>(env.CONTROL_BUCKET, `v1/jobs/${jobMatch[1]}.json`);
    return job ? reply(200, sanitizeJob(job.value)) : reply(404, { error: "NOT_FOUND" });
  }
  const proposalMatch = request.method === "GET" && url.pathname.match(/^\/v1\/proposals\/([^/]+)$/);
  if (proposalMatch) {
    if (!PROPOSAL_ID.test(proposalMatch[1])) return reply(400, { error: "INVALID_IDENTIFIER" });
    const proposal = await readStored<unknown>(env.CONTROL_BUCKET, `v1/proposals/${proposalMatch[1]}.json`);
    return proposal ? reply(200, proposal.value) : reply(404, { error: "NOT_FOUND" });
  }
  return reply(404, { error: "NOT_FOUND" });
}

// ── Vault read-only types ────────────────────────────────────────────

interface VaultReceiptArtifact {
  path: string;
  media_type: string;
  content_digest: string;
  byte_length: number;
}

interface VaultReceipt {
  receipt_version: string;
  publication_plan_id: string;
  publication_plan_object_digest: string;
  capture_id: string;
  artifacts: VaultReceiptArtifact[];
}

interface ManifestArtifact extends VaultReceiptArtifact {
  capture_id: string;
  publication_plan_id: string;
}

interface VaultManifest {
  manifest_version: string;
  artifacts: ManifestArtifact[];
}

const RECEIPT_PREFIX = "v1/publications/";
const DIGEST_RE = /^sha256:[a-f0-9]{64}$/;
const PLAN_ID_RE = /^publication_plan_[a-f0-9]{32}$/;
const CAPTURE_ID_RE = /^capture_[a-f0-9]{32}$/;

function canonicalJsonBytes(obj: unknown): Uint8Array {
  // Recursively sort keys, compact separators, no trailing newline here
  const text = JSON.stringify(obj, function replacer(_key, value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const sorted: Record<string, unknown> = {};
      for (const k of Object.keys(value).sort()) {
        sorted[k] = (value as Record<string, unknown>)[k];
      }
      return sorted;
    }
    return value;
  });
  return new TextEncoder().encode(text + "\n");
}

function validateReceipt(r: Record<string, unknown>): VaultReceipt | null {
  if (r.receipt_version !== "0.1.0") return null;
  if (typeof r.publication_plan_id !== "string" || !PLAN_ID_RE.test(r.publication_plan_id)) return null;
  if (typeof r.publication_plan_object_digest !== "string" || !DIGEST_RE.test(r.publication_plan_object_digest)) return null;
  if (typeof r.capture_id !== "string" || !CAPTURE_ID_RE.test(r.capture_id)) return null;
  if (!Array.isArray(r.artifacts) || r.artifacts.length !== 2) return null;
  const artifacts: VaultReceiptArtifact[] = [];
  for (const a of r.artifacts) {
    if (typeof a !== "object" || a === null) return null;
    const item = a as Record<string, unknown>;
    if (typeof item.path !== "string" || !item.path.startsWith("MedLearn/")) return null;
    if (typeof item.media_type !== "string") return null;
    if (typeof item.content_digest !== "string" || !DIGEST_RE.test(item.content_digest)) return null;
    if (typeof item.byte_length !== "number" || item.byte_length < 1) return null;
    artifacts.push({
      path: item.path,
      media_type: item.media_type,
      content_digest: item.content_digest,
      byte_length: item.byte_length,
    });
  }
  if (artifacts[0].media_type !== "application/json; charset=utf-8") return null;
  if (artifacts[1].media_type !== "text/markdown; charset=utf-8") return null;
  return {
    receipt_version: "0.1.0",
    publication_plan_id: r.publication_plan_id as string,
    publication_plan_object_digest: r.publication_plan_object_digest as string,
    capture_id: r.capture_id as string,
    artifacts,
  };
}

async function listAllReceipts(bucket: R2Bucket): Promise<{ receipts: VaultReceipt[]; error?: string }> {
  const receipts: VaultReceipt[] = [];
  let cursor: string | undefined;
  do {
    const result = await bucket.list({ prefix: RECEIPT_PREFIX, cursor });
    for (const obj of result.objects) {
      if (!obj.key.endsWith(".json")) continue;
      if (!obj.key.startsWith(RECEIPT_PREFIX)) continue;
      const stored = await bucket.get(obj.key);
      if (!stored) continue;
      let parsed: unknown;
      try {
        parsed = await stored.json();
      } catch {
        return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      }
      if (typeof parsed !== "object" || parsed === null) {
        return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      }
      const receipt = validateReceipt(parsed as Record<string, unknown>);
      if (!receipt) {
        return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      }
      // Verify canonical: re-serialize and compare byte-for-byte
      const canonical = canonicalJsonBytes(parsed as Record<string, unknown>);
      const storedBody = new Uint8Array(await stored.arrayBuffer());
      if (storedBody.length !== canonical.length) {
        return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      }
      for (let i = 0; i < storedBody.length; i++) {
        if (storedBody[i] !== canonical[i]) {
          return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
        }
      }
      receipts.push(receipt);
    }
    cursor = result.truncated ? result.cursor : undefined;
  } while (cursor);
  return { receipts };
}

function buildManifest(receipts: VaultReceipt[]): { manifest: VaultManifest; error?: string } {
  const seen = new Map<string, ManifestArtifact>();
  for (const receipt of receipts) {
    for (const artifact of receipt.artifacts) {
      const existing = seen.get(artifact.path);
      if (existing) {
        // Duplicate: must match exactly on all fields
        if (
          existing.content_digest !== artifact.content_digest ||
          existing.byte_length !== artifact.byte_length ||
          existing.media_type !== artifact.media_type
        ) {
          return { manifest: { manifest_version: "0.1.0", artifacts: [] }, error: "VAULT_MANIFEST_CONFLICT" };
        }
        // Identical duplicate — deterministic dedup, keep first
        continue;
      }
      seen.set(artifact.path, {
        ...artifact,
        capture_id: receipt.capture_id,
        publication_plan_id: receipt.publication_plan_id,
      });
    }
  }
  // Sort by path ascending
  const artifacts = [...seen.values()].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return { manifest: { manifest_version: "0.1.0", artifacts } };
}

function validateFilePath(path: string): string | null {
  if (!path || path.length === 0) return "INVALID_VAULT_PATH";
  if (path.length > 1024) return "INVALID_VAULT_PATH";
  if (!path.startsWith("MedLearn/")) return "INVALID_VAULT_PATH";
  if (path.includes("\\")) return "INVALID_VAULT_PATH";
  if (path.includes("//")) return "INVALID_VAULT_PATH";
  if (path.includes("\x00")) return "INVALID_VAULT_PATH";
  if (path.includes("./") || path.includes("/.")) return "INVALID_VAULT_PATH";
  if (path.startsWith("/")) return "INVALID_VAULT_PATH";
  // Check for invalid percent encoding
  try {
    const decoded = decodeURIComponent(path);
    if (decoded !== path) return "INVALID_VAULT_PATH"; // path should already be decoded
  } catch {
    return "INVALID_VAULT_PATH";
  }
  // Check for ".." as path component
  if (path.split("/").some(part => part === ".." || part === ".")) return "INVALID_VAULT_PATH";
  return null;
}

async function routeVault(request: Request, env: Env, url: URL): Promise<Response> {
  const cfg = vaultConfig(env);
  if (!cfg) return secure(reply(503, { error: "VAULT_SERVICE_MISCONFIGURED" }));

  if (!(await authorized(request, cfg.MEDLEARN_SYNC_TOKEN))) return reply(401, { error: "UNAUTHORIZED" });

  // GET /v1/vault/manifest
  if (request.method === "GET" && url.pathname === "/v1/vault/manifest") {
    const { receipts, error } = await listAllReceipts(cfg.VAULT_BUCKET);
    if (error) return secure(reply(503, { error }));

    const { manifest, error: manifestError } = buildManifest(receipts);
    if (manifestError) return secure(reply(503, { error: manifestError }));

    const bodyBytes = canonicalJsonBytes(manifest as unknown as Record<string, unknown>);
    const body = new TextDecoder().decode(bodyBytes);
    const etag = `"sha256:${await sha256(bodyBytes)}"`;

    const ifNoneMatch = request.headers.get("if-none-match");
    if (ifNoneMatch && ifNoneMatch === etag) {
      return new Response(null, {
        status: 304,
        headers: {
          "etag": etag,
          "cache-control": "private, no-cache",
          "vary": "Authorization",
        },
      });
    }

    return new Response(body, {
      status: 200,
      headers: {
        "content-type": "application/json; charset=utf-8",
        "etag": etag,
        "cache-control": "private, no-cache",
        "vary": "Authorization",
      },
    });
  }

  // GET /v1/vault/files?path=<percent-encoded>
  if (request.method === "GET" && url.pathname === "/v1/vault/files") {
    const rawPath = url.searchParams.get("path");
    if (!rawPath) return secure(reply(400, { error: "INVALID_VAULT_PATH" }));

    // Path should already be decoded by URL API, but verify
    const pathError = validateFilePath(rawPath);
    if (pathError) return secure(reply(400, { error: pathError }));

    // Look up in manifest to confirm membership
    const { receipts, error: listError } = await listAllReceipts(cfg.VAULT_BUCKET);
    if (listError) return secure(reply(503, { error: listError }));

    const { manifest } = buildManifest(receipts);
    const match = manifest.artifacts.find(a => a.path === rawPath);
    if (!match) return secure(reply(404, { error: "NOT_FOUND" }));

    // Download from R2
    const object = await cfg.VAULT_BUCKET.get(rawPath);
    if (!object) return secure(reply(503, { error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" }));

    const objectBody = await object.arrayBuffer();

    // Integrity checks
    const actualDigest = "sha256:" + await sha256(objectBody);
    if (actualDigest !== match.content_digest) {
      return secure(reply(503, { error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" }));
    }
    if (objectBody.byteLength !== match.byte_length) {
      return secure(reply(503, { error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" }));
    }
    const objectContentType = object.httpMetadata?.contentType ?? "application/octet-stream";
    if (objectContentType !== match.media_type) {
      return secure(reply(503, { error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" }));
    }

    const artifactEtag = `"${match.content_digest}"`;
    const ifNoneMatch = request.headers.get("if-none-match");
    if (ifNoneMatch && ifNoneMatch === artifactEtag) {
      return new Response(null, {
        status: 304,
        headers: {
          "etag": artifactEtag,
          "cache-control": "private, no-cache",
          "vary": "Authorization",
        },
      });
    }

    return new Response(objectBody, {
      status: 200,
      headers: {
        "content-type": match.media_type,
        "content-length": String(objectBody.byteLength),
        "etag": artifactEtag,
        "cache-control": "private, no-cache",
        "vary": "Authorization",
      },
    });
  }

  return reply(404, { error: "NOT_FOUND" });
}

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/") return reply(200, { service: "medlearn-cloud", status: "ok" });
  if (request.method === "GET" && url.pathname === "/health") return reply(200, { status: "ok" });
  if (!url.pathname.startsWith("/v1/")) return reply(404, { error: "NOT_FOUND" });

  // Vault routes — independent config check, does not affect control routes
  if (url.pathname.startsWith("/v1/vault/")) {
    try {
      return await routeVault(request, env, url);
    } catch {
      console.error(JSON.stringify({ stage: "vault_route", error_code: "VAULT_STORAGE_FAILURE" }));
      return secure(reply(503, { error: "VAULT_STORAGE_UNAVAILABLE" }));
    }
  }

  const configured = config(env);
  if (!configured) return secure(reply(503, { error: "SERVICE_MISCONFIGURED" }));
  try {
    return secure(await routeV1(request, configured, url));
  } catch {
    console.error(JSON.stringify({ stage: "v1_route", error_code: "CONTROL_STORAGE_FAILURE" }));
    return secure(reply(503, { error: "CONTROL_STORAGE_UNAVAILABLE" }));
  }
}

export default { fetch: handle };
