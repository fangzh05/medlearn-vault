import validateEnvelope from "./generated/intake-validator.js";
import validateHandoff from "./generated/handoff-validator.js";
import handoffSchema from "../../schemas/workflow/current/medlearn_handoff.schema.json";

const toolHandoffSchema = {
  ...handoffSchema,
  $id: "https://medlearn.invalid/schemas/medlearn_handoff.schema.json",
};

export interface Env {
  CONTROL_BUCKET?: R2Bucket;
  VAULT_BUCKET?: R2Bucket;
  MEDLEARN_INGEST_TOKEN?: string;
  MEDLEARN_WORK_TOKEN?: string;
  MEDLEARN_SYNC_TOKEN?: string;
  GITHUB_ACTIONS_DISPATCH_TOKEN?: string;
}

interface IntakeConfig {
  CONTROL_BUCKET: R2Bucket;
  GITHUB_ACTIONS_DISPATCH_TOKEN: string;
}

interface Config extends IntakeConfig { MEDLEARN_INGEST_TOKEN: string }

interface VaultConfig {
  VAULT_BUCKET: R2Bucket;
  MEDLEARN_SYNC_TOKEN: string;
}

interface WorkConfig extends IntakeConfig {
  MEDLEARN_WORK_TOKEN: string;
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
const MAX_HANDOFF_BODY = 256 * 1024;
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

function workConfig(env: Env): WorkConfig | null {
  const token = env.MEDLEARN_WORK_TOKEN;
  const github = env.GITHUB_ACTIONS_DISPATCH_TOKEN;
  if (!env.CONTROL_BUCKET || typeof env.CONTROL_BUCKET.get !== "function") return null;
  if (typeof token !== "string" || token.length < 32) return null;
  if (typeof github !== "string" || github.trim().length === 0) return null;
  return { CONTROL_BUCKET: env.CONTROL_BUCKET, MEDLEARN_WORK_TOKEN: token, GITHUB_ACTIONS_DISPATCH_TOKEN: github };
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

async function dispatch(env: IntakeConfig, job: JobRecord): Promise<boolean> {
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

async function dispatchRecoverably(env: IntakeConfig, stored: Stored<JobRecord>, nowMs: number): Promise<Response> {
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

async function submitIntake(
  env: IntakeConfig, body: ArrayBuffer, idempotencyKey: string, nowMs: number,
): Promise<Response> {
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
  const claim = await claimIdempotency(env.CONTROL_BUCKET, idemKey, intakeDigest, new Date(nowMs).toISOString());
  if (claim.intake_digest !== intakeDigest) return reply(409, { error: "IDEMPOTENCY_CONFLICT" });
  const job = await ensureArtifacts(env.CONTROL_BUCKET, claim, intakeKey, body, new Date(nowMs).toISOString());
  return dispatchRecoverably(env, job, nowMs);
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
  return submitIntake(env, body, idempotencyKey, Date.now());
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(value, function replacer(_key, item) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      const sorted: Record<string, unknown> = {};
      for (const key of Object.keys(item as Record<string, unknown>).sort()) sorted[key] = (item as Record<string, unknown>)[key];
      return sorted;
    }
    return item;
  });
}

function handoffSemanticError(handoff: Record<string, unknown>): string | null {
  const messages = handoff.evidence_messages as { local_id: string }[];
  const ids = messages.map((item) => item.local_id);
  if (new Set(ids).size !== ids.length) return "HANDOFF_DUPLICATE_LOCAL_ID";
  const known = new Set(ids);
  const groups: string[][] = [];
  for (const key of ["concepts", "claims", "learner_evidence", "unresolved_questions", "unfinished_topics"] as const) {
    for (const item of handoff[key] as { evidence_local_ids: string[] }[]) groups.push(item.evidence_local_ids);
  }
  for (const item of handoff.misconceptions as { observed_error_local_ids: string[]; correction_local_ids: string[] }[]) {
    groups.push(item.observed_error_local_ids, item.correction_local_ids);
  }
  return groups.some((group) => group.some((id) => !known.has(id))) ? "HANDOFF_DANGLING_EVIDENCE_REFERENCE" : null;
}

async function convertHandoff(handoff: Record<string, unknown>): Promise<{ body: ArrayBuffer; idempotencyKey: string }> {
  if ((handoff.learning_goals as unknown[]).length || (handoff.unfinished_topics as unknown[]).length)
    throw new Error("HANDOFF_CONVERSION_FAILURE");
  const canonical = canonicalJson(handoff);
  const digest = await sha256(canonical);
  const session = handoff.session as Record<string, string | null>;
  const messages = handoff.evidence_messages as Record<string, unknown>[];
  const messageIds = new Map<string, string>();
  await Promise.all(messages.map(async (message) => {
    messageIds.set(message.local_id as string, `message_${(await sha256(`${digest}:${message.local_id as string}`)).slice(0, 32)}`);
  }));
  const refs = (ids: string[]) => ids.map((id) => messageIds.get(id)!);
  const claims = (handoff.claims as Record<string, unknown>[]).map((item) => ({
    statement: item.statement, claim_type: item.claim_type, concept_terms: item.concept_terms,
    evidence_message_ids: refs(item.evidence_local_ids as string[]),
    ...(item.claim_type === "question" && item.question_priority !== null && item.question_priority !== undefined
      ? { question_priority: item.question_priority } : {}),
  }));
  for (const item of handoff.unresolved_questions as Record<string, unknown>[]) {
    claims.push({ statement: item.statement, claim_type: "question", concept_terms: item.concept_terms,
      evidence_message_ids: refs(item.evidence_local_ids as string[]), question_priority: item.question_priority as string });
  }
  const draft = {
    draft_version: "0.3.0",
    context: {
      source_id: `source_${digest.slice(0, 32)}`, session_id: `session_${digest.slice(0, 32)}`,
      discipline_id: session.discipline_id, course_id: session.course_id, chapter_id: session.chapter_id,
      locale: "zh-CN", session_started_at: session.session_started_at, captured_at: session.captured_at,
    },
    evidence_messages: messages.map((item) => ({
      message_id: messageIds.get(item.local_id as string), role: item.role,
      observed_at: item.observed_at ?? session.captured_at, excerpt: item.excerpt,
    })),
    concept_mentions: (handoff.concepts as Record<string, unknown>[]).map((item) => ({
      surface_text: item.name, evidence_message_ids: refs(item.evidence_local_ids as string[]),
      suggested_canonical_name: item.name, suggested_preferred_english: item.preferred_english,
      suggested_concept_type: item.concept_type, suggested_scope_note: item.scope_note,
    })),
    claim_candidates: claims,
    learner_evidence_candidates: (handoff.learner_evidence as Record<string, unknown>[]).map((item) => ({
      concept_terms: item.concept_terms, evidence_type: item.evidence_type, confidence: item.confidence,
      rationale: item.rationale, evidence_message_ids: refs(item.evidence_local_ids as string[]),
    })),
    misconception_candidates: (handoff.misconceptions as Record<string, unknown>[]).map((item) => ({
      observed_error_logic: item.observed_error_logic, concept_terms: item.concept_terms,
      observed_error_message_ids: refs(item.observed_error_local_ids as string[]),
      correction_message_ids: refs(item.correction_local_ids as string[]),
      proposed_correction: item.proposed_correction, correction_terms: item.correction_terms, severity: item.severity,
    })),
  };
  const envelope = { intake_version: "0.1.0", client_kind: "chatgpt_work", draft };
  if (!validateEnvelope(envelope)) throw new Error("HANDOFF_CONVERSION_FAILURE");
  const encoded = new TextEncoder().encode(canonicalJson(envelope));
  return { body: encoded.buffer.slice(encoded.byteOffset, encoded.byteOffset + encoded.byteLength), idempotencyKey: `medlearn-handoff-${digest}` };
}

function mcpError(id: unknown, code: string): Response {
  return reply(200, { jsonrpc: "2.0", id: id ?? null, error: { code: -32602, message: code } });
}

async function routeMcp(request: Request, env: Env): Promise<Response> {
  const configured = workConfig(env);
  if (!configured) return reply(503, { error: "SERVICE_MISCONFIGURED" });
  if (!(await authorized(request, configured.MEDLEARN_WORK_TOKEN))) return reply(401, { error: "HANDOFF_AUTH_REQUIRED" });
  if (request.method !== "POST" || request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json")
    return reply(400, { error: "HANDOFF_INVALID_JSON" });
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_HANDOFF_BODY) return reply(413, { error: "HANDOFF_SCHEMA_INVALID" });
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_HANDOFF_BODY) return reply(413, { error: "HANDOFF_SCHEMA_INVALID" });
  let call: Record<string, unknown>;
  try { call = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as Record<string, unknown>; }
  catch { return reply(400, { error: "HANDOFF_INVALID_JSON" }); }
  const id = call.id ?? null;
  if (call.jsonrpc !== "2.0" || typeof call.method !== "string") return mcpError(id, "HANDOFF_INVALID_JSON");
  if (call.method === "initialize") return reply(200, { jsonrpc: "2.0", id, result: { protocolVersion: "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "medlearn-work", version: "0.14.0" } } });
  if (call.method === "tools/list") return reply(200, { jsonrpc: "2.0", id, result: { tools: [{ name: "submit_learning_handoff", description: "Validate and submit a user-selected MedLearnHandoff 0.1.0 Project Source.", inputSchema: { type: "object", additionalProperties: false, required: ["handoff"], properties: { handoff: toolHandoffSchema } } }] } });
  if (call.method !== "tools/call") return mcpError(id, "HANDOFF_INVALID_JSON");
  const params = call.params as Record<string, unknown> | undefined;
  if (!params || params.name !== "submit_learning_handoff" || typeof params.arguments !== "object" || params.arguments === null)
    return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  const argumentsValue = params.arguments as Record<string, unknown>;
  if (Object.keys(argumentsValue).length !== 1 || !("handoff" in argumentsValue) || typeof argumentsValue.handoff !== "object" || argumentsValue.handoff === null)
    return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  const handoff = argumentsValue.handoff as Record<string, unknown>;
  if (handoff.handoff_version !== "0.1.0") return mcpError(id, "HANDOFF_UNSUPPORTED_VERSION");
  if (!validateHandoff(handoff)) return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  const semantic = handoffSemanticError(handoff);
  if (semantic) return mcpError(id, semantic);
  try {
    const converted = await convertHandoff(handoff);
    const response = await submitIntake(configured, converted.body, converted.idempotencyKey, Date.now());
    if (!response.ok) return mcpError(id, "HANDOFF_SUBMISSION_FAILURE");
    const job = await response.json<JobRecord>();
    const result = {
      status: "submitted", job_id: job.job_id, intake_digest: job.intake_digest,
      concept_count: (handoff.concepts as unknown[]).length, claim_count: (handoff.claims as unknown[]).length,
      learner_evidence_count: (handoff.learner_evidence as unknown[]).length,
      misconception_count: (handoff.misconceptions as unknown[]).length,
      unresolved_count: (handoff.unresolved_questions as unknown[]).length,
      unfinished_count: (handoff.unfinished_topics as unknown[]).length,
    };
    return reply(200, { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result } });
  } catch (error) {
    const code = error instanceof Error && error.message === "HANDOFF_CONVERSION_FAILURE" ? error.message : "HANDOFF_CONVERSION_FAILURE";
    return mcpError(id, code);
  }
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
const RECEIPT_KEY_RE = /^v1\/publications\/(publication_plan_[a-f0-9]{32})\.json$/;
const RECEIPT_KEYS = ["receipt_version", "publication_plan_id", "publication_plan_object_digest", "capture_id", "artifacts"] as const;
const ARTIFACT_KEYS = ["path", "media_type", "content_digest", "byte_length"] as const;

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

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => Object.hasOwn(value, key));
}

function validArtifactPath(path: string, captureId: string, index: number): boolean {
  if (path.includes("\\") || path.includes("//") || path.includes("\x00") || path.includes("%") || path.startsWith("/")) return false;
  if (path.split("/").some((part) => part === "." || part === "..")) return false;
  if (index === 0) return path === `MedLearn/Data/Captures/${captureId}.json`;
  const match = path.match(new RegExp(`^MedLearn/Captures/[0-9]{4}/(0[1-9]|1[0-2])/${captureId}\\.md$`));
  return match !== null;
}

function validateReceipt(r: Record<string, unknown>): VaultReceipt | null {
  if (!hasExactKeys(r, RECEIPT_KEYS)) return null;
  if (r.receipt_version !== "0.1.0") return null;
  if (typeof r.publication_plan_id !== "string" || !PLAN_ID_RE.test(r.publication_plan_id)) return null;
  if (typeof r.publication_plan_object_digest !== "string" || !DIGEST_RE.test(r.publication_plan_object_digest)) return null;
  if (typeof r.capture_id !== "string" || !CAPTURE_ID_RE.test(r.capture_id)) return null;
  if (!Array.isArray(r.artifacts) || r.artifacts.length !== 2) return null;
  const artifacts: VaultReceiptArtifact[] = [];
  for (const [index, a] of r.artifacts.entries()) {
    if (typeof a !== "object" || a === null) return null;
    const item = a as Record<string, unknown>;
    if (!hasExactKeys(item, ARTIFACT_KEYS)) return null;
    if (typeof item.path !== "string" || !validArtifactPath(item.path, r.capture_id, index)) return null;
    if (typeof item.media_type !== "string") return null;
    if (typeof item.content_digest !== "string" || !DIGEST_RE.test(item.content_digest)) return null;
    if (typeof item.byte_length !== "number" || !Number.isInteger(item.byte_length) || item.byte_length < 1) return null;
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
      const keyMatch = obj.key.match(RECEIPT_KEY_RE);
      if (!keyMatch) return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      const stored = await bucket.get(obj.key);
      if (!stored) return { receipts: [], error: "VAULT_STORAGE_UNAVAILABLE" };
      if (stored.httpMetadata?.contentType !== "application/json; charset=utf-8")
        return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      let storedBody: Uint8Array;
      let parsed: unknown;
      try {
        storedBody = new Uint8Array(await stored.arrayBuffer());
        parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(storedBody));
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
      if (receipt.publication_plan_id !== keyMatch[1])
        return { receipts: [], error: "INVALID_VAULT_PUBLICATION_RECEIPT" };
      // Verify canonical: re-serialize and compare byte-for-byte
      const canonical = canonicalJsonBytes(parsed as Record<string, unknown>);
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

export function buildManifest(receipts: VaultReceipt[]): { manifest: VaultManifest; error?: string } {
  const seen = new Map<string, ManifestArtifact>();
  for (const receipt of [...receipts].sort((a, b) => a.publication_plan_id.localeCompare(b.publication_plan_id))) {
    for (const artifact of receipt.artifacts) {
      const existing = seen.get(artifact.path);
      if (existing) {
        // Duplicate: must match exactly on all fields
        if (
          existing.content_digest !== artifact.content_digest ||
          existing.byte_length !== artifact.byte_length ||
          existing.media_type !== artifact.media_type ||
          existing.capture_id !== receipt.capture_id
        ) {
          return { manifest: { manifest_version: "0.1.0", artifacts: [] }, error: "VAULT_MANIFEST_CONFLICT" };
        }
        // Identical duplicate — sorted receipt order makes provenance deterministic.
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

  if (!(await authorized(request, cfg.MEDLEARN_SYNC_TOKEN))) return secure(reply(401, { error: "UNAUTHORIZED" }));

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

    const { manifest, error: manifestError } = buildManifest(receipts);
    if (manifestError) return secure(reply(503, { error: manifestError }));
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

  return secure(reply(404, { error: "NOT_FOUND" }));
}

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/") return reply(200, { service: "medlearn-cloud", status: "ok" });
  if (request.method === "GET" && url.pathname === "/health") return reply(200, { status: "ok" });
  if (url.pathname === "/mcp") {
    try {
      return secure(await routeMcp(request, env));
    } catch {
      console.error(JSON.stringify({ stage: "mcp_route", error_code: "HANDOFF_SUBMISSION_FAILURE" }));
      return secure(reply(503, { error: "HANDOFF_SUBMISSION_FAILURE" }));
    }
  }
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
