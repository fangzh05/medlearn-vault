import validateEnvelope from "./generated/intake-validator.js";
import validateSegment from "./generated/segment-validator.js";
import validateHandoff from "./generated/handoff-validator.js";
import segmentSchema from "../../schemas/workflow/current/learning_segment.schema.json";
import handoffSchema from "../../schemas/workflow/current/medlearn_handoff.schema.json";
import { createRemoteJWKSet, jwtVerify, type JWTPayload, type JWTVerifyGetKey } from "jose";

const toolSegmentSchema = {
  ...segmentSchema,
  $id: "https://medlearn.invalid/schemas/learning_segment.schema.json",
};
const toolHandoffSchema = { ...handoffSchema, $id: "https://medlearn.invalid/schemas/medlearn_handoff.schema.json" };

export interface Env {
  CONTROL_BUCKET?: R2Bucket;
  VAULT_BUCKET?: R2Bucket;
  MEDLEARN_INGEST_TOKEN?: string;
  MEDLEARN_WORK_OAUTH_ISSUER?: string;
  MEDLEARN_WORK_OAUTH_AUDIENCE?: string;
  MEDLEARN_WORK_OAUTH_ALLOWED_SUBJECT?: string;
  MEDLEARN_WORK_OAUTH_RESOURCE?: string;
  MEDLEARN_WORK_ALLOWED_ORIGINS?: string;
  MEDLEARN_SYNC_TOKEN?: string;
  GITHUB_ACTIONS_DISPATCH_TOKEN?: string;
  MEDLEARN_WORK_DISPATCH_MODE?: string;
  DEEPSEEK_API_KEY?: string;
}

type DispatchMode = "github" | "persist_only";

interface WorkConfig {
  CONTROL_BUCKET: R2Bucket;
  dispatchMode: DispatchMode;
  githubToken?: string;
}

interface Config extends WorkConfig { MEDLEARN_INGEST_TOKEN: string }

interface VaultConfig {
  VAULT_BUCKET: R2Bucket;
  MEDLEARN_SYNC_TOKEN: string;
}

interface OAuthConfig {
  issuer: string;
  audience: string;
  subject: string;
  resource: string;
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
  reproposal_of_job_id?: string;
  reproposal_of_proposal_id?: string;
  catalog_update_id?: string;
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
const HANDOFF_CONVERSION_VERSION = "medlearn.handoff_to_intake.v4";
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

function secureMcp(response: Response): Response {
  secure(response).headers.set("vary", "Authorization, Origin");
  return response;
}

function config(env: Env): Config | null {
  const token = env.MEDLEARN_INGEST_TOKEN;
  if (typeof token !== "string" || token.length < 32) return null;
  const work = workConfig(env);
  return work ? { ...work, MEDLEARN_INGEST_TOKEN: token } : null;
}

function vaultConfig(env: Env): VaultConfig | null {
  const token = env.MEDLEARN_SYNC_TOKEN;
  if (!env.VAULT_BUCKET || typeof env.VAULT_BUCKET.get !== "function") return null;
  if (typeof token !== "string" || token.length < 32) return null;
  return { VAULT_BUCKET: env.VAULT_BUCKET, MEDLEARN_SYNC_TOKEN: token };
}

function workConfig(env: Env): WorkConfig | null {
  if (!env.CONTROL_BUCKET || typeof env.CONTROL_BUCKET.get !== "function") return null;
  const dispatchMode = env.MEDLEARN_WORK_DISPATCH_MODE;
  if (dispatchMode !== "github" && dispatchMode !== "persist_only") return null;
  if (dispatchMode === "persist_only") return { CONTROL_BUCKET: env.CONTROL_BUCKET, dispatchMode };
  const githubToken = env.GITHUB_ACTIONS_DISPATCH_TOKEN;
  if (typeof githubToken !== "string" || githubToken.trim().length === 0) return null;
  return { CONTROL_BUCKET: env.CONTROL_BUCKET, dispatchMode, githubToken };
}

function canonicalIssuer(value: string): string | null {
  if (!/^https:\/\//i.test(value)) return null;
  let url: URL;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "https:") return null;
  if (url.username || url.password) return null;
  if (url.search) return null;
  if (url.hash) return null;
  let pathname = url.pathname;
  if (!pathname.endsWith("/")) pathname = pathname + "/";
  return `${url.protocol}//${url.host}${pathname}`;
}

function canonicalResource(value: string): string | null {
  if (!/^https:\/\//i.test(value)) return null;
  let url: URL;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "https:") return null;
  if (url.username || url.password) return null;
  if (url.search) return null;
  if (url.hash) return null;
  const pathname = url.pathname;
  if (pathname !== "/" && pathname !== "") return null;
  if (url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]") return null;
  return `${url.protocol}//${url.host}`;
}

function oauthConfig(env: Env): OAuthConfig | null {
  const issuer = env.MEDLEARN_WORK_OAUTH_ISSUER?.trim();
  const audience = env.MEDLEARN_WORK_OAUTH_AUDIENCE?.trim();
  const subject = env.MEDLEARN_WORK_OAUTH_ALLOWED_SUBJECT?.trim();
  const rawResource = env.MEDLEARN_WORK_OAUTH_RESOURCE?.trim();
  if (!issuer || !audience || !subject) return null;
  const canonical = canonicalIssuer(issuer);
  if (!canonical) return null;
  const resourceRaw = rawResource || audience;
  const resource = canonicalResource(resourceRaw);
  if (!resource) return null;
  if (audience !== resource) return null;
  return { issuer: canonical, audience, subject, resource };
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

interface RawStored { body: ArrayBuffer; etag: string }

async function readRawStored(bucket: R2Bucket, key: string): Promise<RawStored | null> {
  const object = await bucket.get(key);
  return object ? { body: await object.arrayBuffer(), etag: object.etag } : null;
}

function bytesOf(value: ArrayBuffer | Uint8Array): Uint8Array {
  return value instanceof Uint8Array ? value : new Uint8Array(value);
}

export async function equalBytes(left: ArrayBuffer | Uint8Array, right: ArrayBuffer | Uint8Array): Promise<boolean> {
  const a = bytesOf(left);
  const b = bytesOf(right);
  const [aDigest, bDigest] = await Promise.all([sha256(a), sha256(b)]);
  if (a.byteLength !== b.byteLength) return false;
  for (let index = 0; index < a.byteLength; index += 1) if (a[index] !== b[index]) return false;
  return aDigest === bDigest;
}

async function putNew(bucket: R2Bucket, key: string, value: string | ArrayBuffer): Promise<boolean> {
  return (await bucket.put(key, value, { onlyIf: new Headers({ "If-None-Match": "*" }) })) !== null;
}

class IntakeStorageConflict extends Error {
  constructor() { super("INTAKE_STORAGE_CONFLICT"); }
}

async function createOrVerifyIntake(
  bucket: R2Bucket, intakeKey: string, exactBody: ArrayBuffer, expectedDigest: string,
): Promise<void> {
  if (!/^sha256:[a-f0-9]{64}$/.test(expectedDigest)) throw new Error("INVALID_INTAKE_DIGEST");
  const expectedKey = `v1/intakes/sha256/${expectedDigest.slice("sha256:".length)}.json`;
  if (intakeKey !== expectedKey) throw new Error("INVALID_INTAKE_KEY");
  await putNew(bucket, intakeKey, exactBody);
  const stored = await readRawStored(bucket, intakeKey);
  if (!stored) throw new Error("INTAKE_STORAGE_UNAVAILABLE");
  const storedDigest = `sha256:${await sha256(stored.body)}`;
  if (storedDigest !== expectedDigest || !(await equalBytes(stored.body, exactBody)))
    throw new IntakeStorageConflict();
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
  if (job.reproposal_of_job_id !== undefined) clean.reproposal_of_job_id = job.reproposal_of_job_id;
  if (job.reproposal_of_proposal_id !== undefined) clean.reproposal_of_proposal_id = job.reproposal_of_proposal_id;
  if (job.catalog_update_id !== undefined) clean.catalog_update_id = job.catalog_update_id;
  return clean;
}

async function dispatch(env: WorkConfig, job: JobRecord): Promise<boolean> {
  if (env.dispatchMode !== "github" || !env.githubToken) return false;
  try {
    const response = await fetch(
      "https://api.github.com/repos/fangzh05/medlearn-vault/actions/workflows/medlearn-propose.yml/dispatches",
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.githubToken}`,
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

async function dispatchRecoverably(env: WorkConfig, stored: Stored<JobRecord>, nowMs: number): Promise<Response> {
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
  env: WorkConfig, body: ArrayBuffer, idempotencyKey: string, nowMs: number,
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
  try {
    await createOrVerifyIntake(env.CONTROL_BUCKET, intakeKey, body, intakeDigest);
  } catch (error) {
    if (error instanceof IntakeStorageConflict) return reply(409, { error: "INTAKE_STORAGE_CONFLICT" });
    throw error;
  }
  const job = await ensureArtifacts(env.CONTROL_BUCKET, claim, intakeKey, body, new Date(nowMs).toISOString());
  if (env.dispatchMode === "persist_only") return reply(202, sanitizeJob(job.value));
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
  const messages = handoff.evidence_messages as { local_id: string; role: "user" | "assistant"; observed_at: string | null }[];
  const ids = messages.map((item) => item.local_id);
  if (new Set(ids).size !== ids.length) return "HANDOFF_DUPLICATE_LOCAL_ID";
  const roles = new Map(messages.map((item) => [item.local_id, item.role]));
  const known = new Set(ids);
  const groups: string[][] = [];
  for (const key of ["concepts", "claims", "learner_evidence", "unresolved_questions", "unfinished_topics"] as const) {
    for (const item of handoff[key] as { evidence_local_ids: string[] }[]) groups.push(item.evidence_local_ids);
  }
  for (const item of handoff.misconceptions as { observed_error_local_ids: string[]; correction_local_ids: string[] }[]) {
    groups.push(item.observed_error_local_ids, item.correction_local_ids);
  }
  if (groups.some((group) => group.some((id) => !known.has(id)))) return "HANDOFF_DANGLING_EVIDENCE_REFERENCE";
  const assertionGroups = [
    ...(handoff.claims as { evidence_local_ids: string[] }[]).map((item) => item.evidence_local_ids),
    ...(handoff.learner_evidence as { evidence_local_ids: string[] }[]).map((item) => item.evidence_local_ids),
    ...(handoff.unresolved_questions as { evidence_local_ids: string[] }[]).map((item) => item.evidence_local_ids),
  ];
  if (assertionGroups.some((group) => new Set(group.map((id) => roles.get(id))).size !== 1))
    return "HANDOFF_ASSERTION_EVIDENCE_ROLE_CONFLICT";
  if ((handoff.learner_evidence as { evidence_local_ids: string[] }[]).some(
    (item) => item.evidence_local_ids.some((id) => roles.get(id) !== "user"),
  )) return "HANDOFF_LEARNER_EVIDENCE_NOT_USER_OWNED";
  if ((handoff.misconceptions as { observed_error_local_ids: string[] }[]).some(
    (item) => item.observed_error_local_ids.some((id) => roles.get(id) !== "user"),
  )) return "HANDOFF_MISCONCEPTION_EVIDENCE_NOT_USER_OWNED";
  const session = handoff.session as { session_started_at: string; captured_at: string };
  const started = Date.parse(session.session_started_at);
  const captured = Date.parse(session.captured_at);
  if (messages.some((item) => {
    const observed = Date.parse(item.observed_at ?? session.captured_at);
    return observed < started || observed > captured;
  })) return "HANDOFF_EVIDENCE_TIME_OUT_OF_RANGE";
  return null;
}

function segmentSemanticError(segment: Record<string, unknown>): string | null {
  const handoff = segment.handoff as Record<string, unknown>;
  const messages = handoff.evidence_messages as { local_id: string }[];
  if (segment.segment_message_count !== messages.length) return "SEGMENT_MESSAGE_COUNT_MISMATCH";
  const markers = messages.map((item) => item.local_id);
  const first = markers.indexOf(segment.first_evidence_marker as string);
  const last = markers.indexOf(segment.last_evidence_marker as string);
  if (segment.coverage_status === "complete" && (first < 0 || last < 0)) return "SEGMENT_MARKER_NOT_FOUND";
  if (segment.coverage_status === "complete" && first > last) return "SEGMENT_MARKER_ORDER_INVALID";
  if (segment.coverage_status !== "complete" && !segment.coverage_note)
    return "SEGMENT_COVERAGE_NOTE_REQUIRED";
  if ((segment.segment_index === 0) !== (segment.previous_segment_digest === null))
    return "SEGMENT_PREDECESSOR_INVALID";
  return handoffSemanticError(handoff);
}

const LEARNING_CHAT_SOURCE_IDENTITY_VERSION = "medlearn.learning_chat_source.v2";

function sourceIdentityField(value: string | null): Uint8Array {
  if (value === null) return new TextEncoder().encode("N");
  const encoded = new TextEncoder().encode(value);
  const header = new TextEncoder().encode(`S${encoded.byteLength}:`);
  const result = new Uint8Array(header.byteLength + encoded.byteLength);
  result.set(header);
  result.set(encoded, header.byteLength);
  return result;
}

function learningChatSourceIdentityPayload(context: {
  session_id: string; discipline_id: string; course_id: string | null; chapter_id: string | null;
  locale: string; session_started_at: string; captured_at: string;
}): Uint8Array {
  const fields = [
    new TextEncoder().encode(LEARNING_CHAT_SOURCE_IDENTITY_VERSION),
    sourceIdentityField(context.discipline_id),
    sourceIdentityField(context.course_id), sourceIdentityField(context.chapter_id),
    sourceIdentityField(context.locale),
  ];
  const length = fields.reduce((total, field) => total + field.byteLength, fields.length - 1);
  const result = new Uint8Array(length);
  let offset = 0;
  for (const field of fields) {
    result.set(field, offset);
    offset += field.byteLength;
    if (offset < result.byteLength) result[offset++] = 0;
  }
  return result;
}

export async function learningChatSourceId(context: {
  session_id: string; discipline_id: string; course_id: string | null; chapter_id: string | null;
  locale: string; session_started_at: string; captured_at: string;
}): Promise<string> {
  return `source_${(await sha256(learningChatSourceIdentityPayload(context))).slice(0, 32)}`;
}

export async function convertHandoff(handoff: Record<string, unknown>): Promise<{ body: ArrayBuffer; idempotencyKey: string }> {
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
  const context = {
    session_id: `session_${digest.slice(0, 32)}`,
    discipline_id: session.discipline_id as string, course_id: session.course_id,
    chapter_id: session.chapter_id, locale: "zh-CN",
    session_started_at: session.session_started_at as string, captured_at: session.captured_at as string,
  };
  const draft = {
    draft_version: "0.3.0",
    context: {
      source_id: await learningChatSourceId(context), ...context,
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
    learner_evidence_candidates: (handoff.learner_evidence as Record<string, unknown>[]).flatMap((item) => {
      const groups = (item.concept_terms as string[]).map((term) => [term]);
      return (groups.length > 0 ? groups : [[]]).map((conceptTerms) => ({
        concept_terms: conceptTerms, evidence_type: item.evidence_type, confidence: item.confidence,
        rationale: item.rationale, evidence_message_ids: refs(item.evidence_local_ids as string[]),
      }));
    }),
    misconception_candidates: (handoff.misconceptions as Record<string, unknown>[]).map((item) => ({
      observed_error_logic: item.observed_error_logic, concept_terms: item.concept_terms,
      observed_error_message_ids: refs(item.observed_error_local_ids as string[]),
      correction_message_ids: refs(item.correction_local_ids as string[]),
      proposed_correction: item.proposed_correction, correction_terms: item.correction_terms, severity: item.severity,
    })),
  };
  const envelope = { intake_version: "0.1.0", client_kind: "chatgpt_work", draft };
  if (!validateEnvelope(envelope)) throw new Error("HANDOFF_CONVERSION_FAILURE");
  const encoded = new TextEncoder().encode(`${canonicalJson(envelope)}\n`);
  const versionSuffix = HANDOFF_CONVERSION_VERSION.split(".").pop()!; // "v4"
  return { body: encoded.buffer.slice(encoded.byteOffset, encoded.byteOffset + encoded.byteLength), idempotencyKey: `medlearn-handoff-${versionSuffix}-${digest}` };
}

const MCP_SCOPE = "medlearn:handoff:submit";
const MCP_INSTRUCTIONS = "This server submits one locally validated LearningSegment containing an explicit MedLearnHandoff. Do not infer missing evidence, scan chats, auto-approve, or auto-publish.";
const jwks = new Map<string, JWTVerifyGetKey>();

function mcpError(id: unknown, code: string, rpcCode = -32602): Response {
  return reply(200, { jsonrpc: "2.0", id: id ?? null, error: { code: rpcCode, message: code } });
}

function mcpTool(): Record<string, unknown> {
  return {
    name: "submit_learning_handoff", title: "Submit MedLearn learning handoff",
    description: "Use only for one locally validated visible LearningSegment 0.2.0. Validates and idempotently submits its nested MedLearnHandoff to the bounded MedLearn intake workflow. Do not use project memory or infer missing evidence.",
    annotations: { readOnlyHint: false, openWorldHint: false, destructiveHint: false },
    securitySchemes: [{ type: "oauth2", scopes: [MCP_SCOPE] }],
    inputSchema: { oneOf: [
      { type: "object", additionalProperties: false, required: ["segment"], properties: { segment: toolSegmentSchema } },
      { type: "object", additionalProperties: false, required: ["handoff"], properties: { handoff: toolHandoffSchema } },
    ] },
    outputSchema: { type: "object", additionalProperties: false, required: ["learning_session_id", "segment_index", "segment_digest", "coverage_status", "status", "job_id", "intake_digest", "concept_count", "claim_count", "learner_evidence_count", "misconception_count", "unresolved_count", "unfinished_count"], properties: {
      learning_session_id: { type: "string" }, segment_index: { type: "integer", minimum: 0 }, segment_digest: { pattern: "^sha256:[a-f0-9]{64}$", type: "string" }, coverage_status: { enum: ["complete", "partial", "unknown"] }, status: { const: "submitted", type: "string" }, job_id: { type: "string" }, intake_digest: { pattern: "^sha256:[a-f0-9]{64}$", type: "string" },
      concept_count: { type: "integer", minimum: 0 }, claim_count: { type: "integer", minimum: 0 }, learner_evidence_count: { type: "integer", minimum: 0 }, misconception_count: { type: "integer", minimum: 0 }, unresolved_count: { type: "integer", minimum: 0 }, unfinished_count: { type: "integer", minimum: 0 },
    } },
  };
}

function authChallenge(metadataUrl: string): string {
  return `Bearer resource_metadata="${metadataUrl}", error="insufficient_scope", error_description="${MCP_SCOPE} is required"`;
}

function mcpAuthError(id: unknown, metadataUrl: string): Response {
  const response = reply(200, { jsonrpc: "2.0", id: id ?? null, result: { content: [{ type: "text", text: "Authentication required." }], _meta: { "mcp/www_authenticate": [authChallenge(metadataUrl)] }, isError: true } });
  response.headers.set("www-authenticate", authChallenge(metadataUrl));
  return response;
}

function allowedOrigin(request: Request, env: Env): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return true;
  const origins = (env.MEDLEARN_WORK_ALLOWED_ORIGINS ?? "https://chatgpt.com,https://chat.openai.com").split(",").map((value) => value.trim());
  return origins.includes(origin);
}

async function accessTokenPayload(request: Request, env: Env): Promise<JWTPayload | null> {
  const oauth = oauthConfig(env);
  const token = request.headers.get("authorization")?.match(/^Bearer\s+(.+)$/i)?.[1];
  if (!oauth || !token) return null;
  try {
    let key = jwks.get(oauth.issuer);
    if (!key) {
      const discoveryUrl = new URL(".well-known/openid-configuration", oauth.issuer);
      const metadata = await (await fetch(discoveryUrl)).json() as { jwks_uri?: string };
      if (!metadata.jwks_uri) return null;
      const jwksUrl = new URL(metadata.jwks_uri);
      if (jwksUrl.protocol !== "https:") return null;
      key = createRemoteJWKSet(jwksUrl);
      jwks.set(oauth.issuer, key);
    }
    const { payload } = await jwtVerify(token, key, { issuer: oauth.issuer });
    const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
    if (!audiences.includes(oauth.audience) && payload.resource !== oauth.audience) return null;
    const scopes = typeof payload.scope === "string" ? payload.scope.split(/\s+/) : [];
    if (payload.sub !== oauth.subject || !scopes.includes(MCP_SCOPE)) return null;
    return payload;
  } catch { return null; }
}

async function routeMcp(request: Request, env: Env): Promise<Response> {
  if (request.method !== "POST") return new Response(null, { status: 405, headers: { allow: "POST" } });
  if (!allowedOrigin(request, env)) return reply(403, { error: "MCP_ORIGIN_FORBIDDEN" });
  const accept = request.headers.get("accept");
  if (accept && !accept.includes("application/json") && !accept.includes("text/event-stream") && !accept.includes("*/*")) return reply(406, { error: "MCP_NOT_ACCEPTABLE" });
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json")
    return reply(415, { error: "HANDOFF_INVALID_JSON" });
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_HANDOFF_BODY) return reply(413, { error: "HANDOFF_SCHEMA_INVALID" });
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_HANDOFF_BODY) return reply(413, { error: "HANDOFF_SCHEMA_INVALID" });
  let call: Record<string, unknown>;
  try { call = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as Record<string, unknown>; }
  catch { return reply(400, { error: "HANDOFF_INVALID_JSON" }); }
  if (Array.isArray(call)) return mcpError(null, "BATCH_NOT_SUPPORTED", -32600);
  const id = call.id ?? null;
  if (call.jsonrpc !== "2.0" || typeof call.method !== "string") return mcpError(id, "HANDOFF_INVALID_JSON");
  const notification = !("id" in call);
  const notify = (): Response => new Response(null, { status: 202 });
  if (call.method === "initialize") return notification ? notify() : reply(200, { jsonrpc: "2.0", id, result: { protocolVersion: "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "medlearn-work", version: "0.15.0" }, instructions: MCP_INSTRUCTIONS } });
  if (call.method === "notifications/initialized") return notify();
  if (call.method === "ping") return notification ? notify() : reply(200, { jsonrpc: "2.0", id, result: {} });
  if (call.method === "tools/list") return notification ? notify() : reply(200, { jsonrpc: "2.0", id, result: { tools: [mcpTool()] } });
  if (call.method !== "tools/call") return mcpError(id, "HANDOFF_INVALID_JSON");
  if (notification) return notify();
  const oauth = oauthConfig(env);
  const metadataUrl = oauth ? `${oauth.resource}/.well-known/oauth-protected-resource` : "";
  if (!(await accessTokenPayload(request, env))) return mcpAuthError(id, metadataUrl);
  const configured = workConfig(env);
  if (!configured) return mcpError(id, "SERVICE_MISCONFIGURED", -32603);
  const params = call.params as Record<string, unknown> | undefined;
  if (!params || params.name !== "submit_learning_handoff" || typeof params.arguments !== "object" || params.arguments === null)
    return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  const argumentsValue = params.arguments as Record<string, unknown>;
  if (Object.keys(argumentsValue).length !== 1 || (!("segment" in argumentsValue) && !("handoff" in argumentsValue)))
    return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  const suppliedSegment = argumentsValue.segment as Record<string, unknown> | undefined;
  if (suppliedSegment && (suppliedSegment.segment_version !== "0.2.0" || !validateSegment(suppliedSegment))) return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  if (suppliedSegment && segmentSemanticError(suppliedSegment)) return mcpError(id, segmentSemanticError(suppliedSegment)!);
  const handoff = (suppliedSegment?.handoff ?? argumentsValue.handoff) as Record<string, unknown>;
  if (handoff.handoff_version !== "0.1.0") return mcpError(id, "HANDOFF_UNSUPPORTED_VERSION");
  const legacySemantic = !suppliedSegment ? handoffSemanticError(handoff) : null;
  if (legacySemantic) return mcpError(id, legacySemantic);
  if (!validateHandoff(handoff)) return mcpError(id, "HANDOFF_SCHEMA_INVALID");
  try {
    const converted = await convertHandoff(handoff);
    const response = await submitIntake(configured, converted.body, converted.idempotencyKey, Date.now());
    if (!response.ok) {
      const failure = await response.json<{ error?: string }>();
      if (failure.error === "INTAKE_STORAGE_CONFLICT") return mcpError(id, "INTAKE_STORAGE_CONFLICT");
      return mcpError(id, "HANDOFF_SUBMISSION_FAILURE");
    }
    const job = await response.json<JobRecord>();
    const result = {
      ...(suppliedSegment ? { learning_session_id: suppliedSegment.learning_session_id, segment_index: suppliedSegment.segment_index,
      segment_digest: `sha256:${await sha256(canonicalJson(suppliedSegment))}`, coverage_status: suppliedSegment.coverage_status } : {}),
      status: "submitted", job_id: job.job_id, intake_digest: job.intake_digest,
      concept_count: (handoff.concepts as unknown[]).length, claim_count: (handoff.claims as unknown[]).length,
      learner_evidence_count: (handoff.learner_evidence as unknown[]).length,
      misconception_count: (handoff.misconceptions as unknown[]).length,
      unresolved_count: (handoff.unresolved_questions as unknown[]).length,
      unfinished_count: (handoff.unfinished_topics as unknown[]).length,
    };
    return reply(200, { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result } });
  } catch (error) {
    if (error instanceof Error && error.message === "HANDOFF_CONVERSION_FAILURE") return mcpError(id, error.message);
    return mcpError(id, "HANDOFF_SUBMISSION_FAILURE");
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

async function createGeneratedNote(request: Request, env: Env): Promise<Response> {
  if (!(await authorized(request, env.MEDLEARN_INGEST_TOKEN ?? ""))) return reply(401, { error: "UNAUTHORIZED" });
  if (!env.VAULT_BUCKET || !env.DEEPSEEK_API_KEY) return reply(503, { error: "NOTE_GENERATION_MISCONFIGURED" });
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") return reply(415, { error: "INVALID_CONTENT_TYPE" });
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return reply(413, { error: "BODY_TOO_LARGE" });
  let input: { markdown?: unknown; title?: unknown };
  try { input = JSON.parse(new TextDecoder().decode(body)); } catch { return reply(400, { error: "INVALID_JSON" }); }
  if (typeof input.markdown !== "string" || !input.markdown.trim() || input.markdown.length > 500_000) return reply(400, { error: "INVALID_MARKDOWN" });
  const title = typeof input.title === "string" && /^[\w\-\u4e00-\u9fff]{1,80}$/.test(input.title) ? input.title : "note";
  const api = await fetch("https://api.deepseek.com/chat/completions", { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${env.DEEPSEEK_API_KEY}` }, body: JSON.stringify({ model: "deepseek-chat", messages: [{ role: "system", content: "Generate a concise medical learning note in Markdown." }, { role: "user", content: input.markdown }] }) });
  if (!api.ok) return reply(502, { error: "NOTE_GENERATION_FAILED" });
  let generated: string;
  try { generated = ((await api.json()) as { choices?: Array<{ message?: { content?: unknown } }> }).choices?.[0]?.message?.content as string; } catch { return reply(502, { error: "NOTE_GENERATION_FAILED" }); }
  if (typeof generated !== "string" || !generated.trim()) return reply(502, { error: "NOTE_GENERATION_FAILED" });
  const id = await sha256(body);
  const path = `MedLearn/Generated/${title}-${id.slice(0, 16)}.md`;
  const bytes = new TextEncoder().encode(generated);
  await env.VAULT_BUCKET.put(path, bytes, { httpMetadata: { contentType: "text/markdown; charset=utf-8" } });
  const record = { path, media_type: "text/markdown; charset=utf-8", content_digest: `sha256:${await sha256(bytes)}`, byte_length: bytes.byteLength };
  await env.VAULT_BUCKET.put(`v1/generated/${id}.json`, canonicalJsonBytes(record), { httpMetadata: { contentType: "application/json; charset=utf-8" } });
  return reply(201, { status: "generated", path });
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
  capture_id?: string;
  publication_plan_id?: string;
  presentation_generation_id?: string;
  storage_key?: string;
}

interface VaultManifest {
  manifest_version: string;
  presentation_generation_id?: string;
  presentation_receipt_digest?: string;
  previous_generation_id?: string | null;
  artifacts: ManifestArtifact[];
}

interface PresentationReceipt {
  presentation_version: string;
  presentation_generation_id: string;
  artifacts: VaultReceiptArtifact[];
}

const RECEIPT_PREFIX = "v1/publications/";
const PRESENTATION_PREFIX = "v1/presentations/";
const DIGEST_RE = /^sha256:[a-f0-9]{64}$/;
const PLAN_ID_RE = /^publication_plan_[a-f0-9]{32}$/;
const CAPTURE_ID_RE = /^capture_[a-f0-9]{32}$/;
const PRESENTATION_ID_RE = /^presentation_[a-f0-9]{32}$/;
const RECEIPT_KEY_RE = /^v1\/publications\/(publication_plan_[a-f0-9]{32})\.json$/;
const PRESENTATION_KEY_RE = /^v1\/presentations\/(presentation_[a-f0-9]{32})\.json$/;
const RECEIPT_KEYS = ["receipt_version", "publication_plan_id", "publication_plan_object_digest", "capture_id", "artifacts"] as const;
const PRESENTATION_KEYS = ["presentation_version", "presentation_generation_id", "artifacts"] as const;
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

/** Presentation receipts are rebuildable and intentionally supersede the
 * legacy immutable publication layout in the user-facing manifest. */
export function buildPresentationManifest(receipts: PresentationReceipt[]): { manifest: VaultManifest; error?: string } {
  const seen = new Map<string, ManifestArtifact>();
  for (const receipt of [...receipts].sort((a, b) => a.presentation_generation_id.localeCompare(b.presentation_generation_id))) {
    if (receipt.presentation_version !== "1.0.0" || !PRESENTATION_ID_RE.test(receipt.presentation_generation_id)) {
      return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_RECEIPT" };
    }
    for (const artifact of receipt.artifacts) {
      if (
        artifact.media_type !== "text/markdown; charset=utf-8" ||
        !(artifact.path.startsWith("MedLearn/学习记录/") || artifact.path.startsWith("MedLearn/概念/")) ||
        !artifact.path.endsWith(".md")
      ) return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_RECEIPT" };
      const existing = seen.get(artifact.path);
      if (existing && (existing.content_digest !== artifact.content_digest || existing.byte_length !== artifact.byte_length)) {
        return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "VAULT_MANIFEST_CONFLICT" };
      }
      seen.set(artifact.path, { ...artifact, presentation_generation_id: receipt.presentation_generation_id });
    }
  }
  return { manifest: { manifest_version: "0.2.0", artifacts: [...seen.values()].sort((a, b) => a.path.localeCompare(b.path)) } };
}

function validatePresentationReceipt(r: Record<string, unknown>): PresentationReceipt | null {
  if (!hasExactKeys(r, PRESENTATION_KEYS) || r.presentation_version !== "1.0.0") return null;
  if (typeof r.presentation_generation_id !== "string" || !PRESENTATION_ID_RE.test(r.presentation_generation_id)) return null;
  if (!Array.isArray(r.artifacts) || r.artifacts.length === 0) return null;
  const artifacts: VaultReceiptArtifact[] = [];
  for (const artifact of r.artifacts) {
    if (typeof artifact !== "object" || artifact === null || !hasExactKeys(artifact as Record<string, unknown>, ARTIFACT_KEYS)) return null;
    const item = artifact as Record<string, unknown>;
    if (
      typeof item.path !== "string" ||
      !item.path.startsWith("MedLearn/") ||
      ( !item.path.startsWith("MedLearn/学习记录/") && !item.path.startsWith("MedLearn/概念/") ) ||
      !item.path.endsWith(".md") || item.path.includes("\\") || item.path.includes("..") ||
      item.media_type !== "text/markdown; charset=utf-8" ||
      typeof item.content_digest !== "string" || !DIGEST_RE.test(item.content_digest) ||
      typeof item.byte_length !== "number" || !Number.isInteger(item.byte_length) || item.byte_length < 1
    ) return null;
    artifacts.push(item as unknown as VaultReceiptArtifact);
  }
  return { presentation_version: "1.0.0", presentation_generation_id: r.presentation_generation_id, artifacts };
}

export async function listAllPresentationReceipts(bucket: R2Bucket): Promise<{ receipts: PresentationReceipt[]; error?: string }> {
  const receipts: PresentationReceipt[] = [];
  let cursor: string | undefined;
  do {
    const result = await bucket.list({ prefix: PRESENTATION_PREFIX, cursor });
    for (const obj of result.objects) {
      const match = obj.key.match(PRESENTATION_KEY_RE);
      if (!match) return { receipts: [], error: "INVALID_PRESENTATION_RECEIPT" };
      const stored = await bucket.get(obj.key);
      if (!stored || stored.httpMetadata?.contentType !== "application/json; charset=utf-8") return { receipts: [], error: "INVALID_PRESENTATION_RECEIPT" };
      let body: Uint8Array;
      let parsed: unknown;
      try {
        body = new Uint8Array(await stored.arrayBuffer());
        parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(body));
      } catch { return { receipts: [], error: "INVALID_PRESENTATION_RECEIPT" }; }
      if (typeof parsed !== "object" || parsed === null) return { receipts: [], error: "INVALID_PRESENTATION_RECEIPT" };
      const receipt = validatePresentationReceipt(parsed as Record<string, unknown>);
      const canonical = canonicalJsonBytes(parsed);
      if (!receipt || receipt.presentation_generation_id !== match[1] || body.length !== canonical.length || body.some((byte, index) => byte !== canonical[index])) {
        return { receipts: [], error: "INVALID_PRESENTATION_RECEIPT" };
      }
      receipts.push(receipt);
    }
    cursor = result.truncated ? result.cursor : undefined;
  } while (cursor);
  return { receipts };
}

async function visibleManifest(bucket: R2Bucket): Promise<{ manifest: VaultManifest; error?: string }> {
  const pointerObject = await bucket.get("v1/presentation-current.json");
  if (pointerObject) {
    let pointerBody: Uint8Array; let pointer: Record<string, unknown>;
    try { pointerBody = new Uint8Array(await pointerObject.arrayBuffer()); pointer = JSON.parse(new TextDecoder().decode(pointerBody)) as Record<string, unknown>; } catch { return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_POINTER" }; }
    if (!hasExactKeys(pointer, ["pointer_version", "active_presentation_generation_id", "presentation_receipt_object_digest", "previous_generation_id"]) || pointer.pointer_version !== "1.0.0" || typeof pointer.active_presentation_generation_id !== "string" || !PRESENTATION_ID_RE.test(pointer.active_presentation_generation_id) || typeof pointer.presentation_receipt_object_digest !== "string" || !DIGEST_RE.test(pointer.presentation_receipt_object_digest) || !pointerBody.every((b, i) => b === canonicalJsonBytes(pointer)[i])) return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_POINTER" };
    const receiptObject = await bucket.get(`v1/presentation-generations/${pointer.active_presentation_generation_id}/receipt.json`);
    if (!receiptObject) return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_RECEIPT" };
    const receiptBody = new Uint8Array(await receiptObject.arrayBuffer());
    if (`sha256:${await sha256(receiptBody)}` !== pointer.presentation_receipt_object_digest) return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_RECEIPT" };
    try {
      const receipt = JSON.parse(new TextDecoder().decode(receiptBody)) as { presentation_generation_id?: string; artifacts?: Array<VaultReceiptArtifact & { storage_key?: string }> };
      if (receipt.presentation_generation_id !== pointer.active_presentation_generation_id || !Array.isArray(receipt.artifacts)) throw new Error();
      const artifacts = receipt.artifacts.map(item => ({ ...item, presentation_generation_id: receipt.presentation_generation_id, storage_key: item.storage_key }));
      if (artifacts.some(item => !item.storage_key || !item.path.startsWith("MedLearn/") || !item.path.endsWith(".md"))) throw new Error();
      const legacy = await listAllReceipts(bucket);
      if (legacy.error) return { manifest: { manifest_version: "0.3.0", artifacts: [] }, error: legacy.error };
      const legacyManifest = buildManifest(legacy.receipts);
      if (legacyManifest.error) return { manifest: { manifest_version: "0.3.0", artifacts: [] }, error: legacyManifest.error };
      const combined = new Map<string, ManifestArtifact>();
      for (const artifact of legacyManifest.manifest.artifacts) combined.set(artifact.path, artifact);
      for (const artifact of artifacts) {
        const existing = combined.get(artifact.path);
        if (existing && JSON.stringify(existing) !== JSON.stringify(artifact)) return { manifest: { manifest_version: "0.3.0", artifacts: [] }, error: "VAULT_MANIFEST_CONFLICT" };
        combined.set(artifact.path, artifact);
      }
      return { manifest: {
        manifest_version: "0.3.0",
        presentation_generation_id: pointer.active_presentation_generation_id,
        presentation_receipt_digest: pointer.presentation_receipt_object_digest,
        previous_generation_id: pointer.previous_generation_id as string | null,
        artifacts: [...combined.values()].sort((a, b) => a.path.localeCompare(b.path)),
      } };
    } catch { return { manifest: { manifest_version: "0.2.0", artifacts: [] }, error: "INVALID_PRESENTATION_RECEIPT" }; }
  }
  const legacy = await listAllReceipts(bucket);
  if (legacy.error) return { manifest: { manifest_version: "0.1.0", artifacts: [] }, error: legacy.error };
  const base = buildManifest(legacy.receipts);
  if (base.error) return base;
  const generated = await bucket.list({ prefix: "v1/generated/" });
  for (const item of generated.objects) {
    const stored = await bucket.get(item.key);
    if (!stored) return { manifest: { manifest_version: "0.1.0", artifacts: [] }, error: "VAULT_STORAGE_UNAVAILABLE" };
    const value = await stored.json<VaultReceiptArtifact>();
    if (!value.path.startsWith("MedLearn/Generated/") || value.media_type !== "text/markdown; charset=utf-8" || !DIGEST_RE.test(value.content_digest)) return { manifest: { manifest_version: "0.1.0", artifacts: [] }, error: "INVALID_GENERATED_NOTE" };
    base.manifest.artifacts.push(value);
  }
  base.manifest.artifacts.sort((a, b) => a.path.localeCompare(b.path));
  return base;
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
    const { manifest, error } = await visibleManifest(cfg.VAULT_BUCKET);
    if (error) return secure(reply(503, { error }));

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
    const { manifest, error } = await visibleManifest(cfg.VAULT_BUCKET);
    if (error) return secure(reply(503, { error }));
    const match = manifest.artifacts.find(a => a.path === rawPath);
    if (!match) return secure(reply(404, { error: "NOT_FOUND" }));

    // Download from R2
    const object = await cfg.VAULT_BUCKET.get(match.storage_key ?? rawPath);
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
  if (request.method === "GET" && url.pathname === "/.well-known/oauth-protected-resource") {
    const oauth = oauthConfig(env);
    return secureMcp(reply(200, {
      resource: oauth?.resource ?? "",
      authorization_servers: oauth ? [oauth.issuer] : [],
      scopes_supported: [MCP_SCOPE],
      resource_documentation: "https://github.com/fangzh05/medlearn-vault/blob/main/docs/medlearn-plugin-setup.md",
    }));
  }
  if (url.pathname === "/mcp") {
    try {
      return secureMcp(await routeMcp(request, env));
    } catch {
      console.error(JSON.stringify({ stage: "mcp_route", error_code: "HANDOFF_SUBMISSION_FAILURE" }));
      return secureMcp(reply(503, { error: "HANDOFF_SUBMISSION_FAILURE" }));
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
  if (request.method === "POST" && url.pathname === "/v1/notes") {
    try { return secure(await createGeneratedNote(request, env)); }
    catch { return secure(reply(503, { error: "NOTE_GENERATION_UNAVAILABLE" })); }
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
