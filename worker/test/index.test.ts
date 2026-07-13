import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import jobSchema from "../../schemas/control/current/job_record.schema.json";
import { buildManifest, handle, transitionJob, type Env, type JobRecord, type Stored } from "../src/index";
import intakeSchema from "../../schemas/workflow/current/intake_envelope.schema.json";
import handoffSchema from "../../schemas/workflow/current/medlearn_handoff.schema.json";

class Bucket {
  objects = new Map<string, { text: string; etag: string; contentType?: string }>();
  getCalls = new Map<string, number>();
  bodyConsumes = new Map<string, number>();
  listedMissing = new Set<string>();
  failNextPrefix?: string;
  private revision = 0;

  async get(key: string) {
    this.getCalls.set(key, (this.getCalls.get(key) ?? 0) + 1);
    const object = this.objects.get(key);
    if (object === undefined) return null;
    let bodyUsed = false;
    const consume = () => {
      if (bodyUsed) throw new Error("R2 body already consumed");
      bodyUsed = true;
      this.bodyConsumes.set(key, (this.bodyConsumes.get(key) ?? 0) + 1);
    };
    return {
      etag: object.etag,
      httpEtag: `"${object.etag}"`,
      httpMetadata: { contentType: object.contentType ?? "application/octet-stream" },
      get bodyUsed() { return bodyUsed; },
      json: async <T>() => { consume(); return JSON.parse(object.text) as T; },
      text: async () => { consume(); return object.text; },
      blob: async () => { consume(); return new Blob([object.text]); },
      arrayBuffer: async () => { consume(); return new TextEncoder().encode(object.text).buffer as ArrayBuffer; },
    };
  }

  async put(
    key: string, value: string | ArrayBuffer,
    options?: { onlyIf?: { etagMatches?: string } | Headers },
  ) {
    if (this.failNextPrefix && key.startsWith(this.failNextPrefix)) {
      this.failNextPrefix = undefined;
      throw new Error("injected storage failure with sensitive details");
    }
    const current = this.objects.get(key);
    const condition = options?.onlyIf;
    if (condition instanceof Headers && condition.get("If-None-Match") === "*" && current) return null;
    if (!(condition instanceof Headers) && condition?.etagMatches && current?.etag !== condition.etagMatches) return null;
    const text = typeof value === "string" ? value : new TextDecoder().decode(value);
    const stored = { text, etag: `"etag-${++this.revision}"` };
    this.objects.set(key, stored);
    return { key, etag: stored.etag, httpEtag: `"${stored.etag}"` };
  }

  async list(opts?: { prefix?: string; cursor?: string }) {
    const prefix = opts?.prefix ?? "";
    const keys = [...new Set([...this.objects.keys(), ...this.listedMissing])].filter(k => k.startsWith(prefix)).sort();
    const cursorIdx = opts?.cursor ? keys.indexOf(opts.cursor) + 1 : 0;
    const pageKeys = keys.slice(cursorIdx, cursorIdx + 3); // page size 3 for pagination tests
    // Cursor is the LAST item in this page (R2 convention)
    const nextCursor = pageKeys.length > 0 ? pageKeys[pageKeys.length - 1] : undefined;
    const moreAvailable = cursorIdx + pageKeys.length < keys.length;
    const listObjects = pageKeys.map(key => ({ key }));
    return {
      objects: listObjects,
      truncated: moreAvailable,
      cursor: moreAvailable ? nextCursor : undefined,
    };
  }

  seed(key: string, text: string, contentType?: string) {
    this.objects.set(key, {
      text,
      etag: `"etag-${++this.revision}"`,
      contentType: contentType ?? (key.startsWith("v1/publications/") ? "application/json; charset=utf-8" : undefined),
    });
  }
}


const token = "ingest-secret-that-is-at-least-32-bytes";
const workToken = "work-secret-that-is-at-least-32-bytes";
const fixtureBytes = readFileSync(resolve("../examples/intake/manual-copd.json"));
const fixtureText = fixtureBytes.toString("utf8");
let bucket: Bucket;
let env: Env;
let dispatch: ReturnType<typeof vi.fn>;

function request(path: string, init: RequestInit = {}) {
  return new Request(`https://example.test${path}`, init);
}

function capture(body: string = fixtureText, key = "key-1") {
  return request("/v1/captures", {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "idempotency-key": key,
    },
    body,
  });
}

function handoff() {
  return {
    handoff_version: "0.1.0",
    session: { title: "血液系统复习", discipline_id: "medicine", course_id: "internal_medicine", chapter_id: "hematology", session_started_at: "2026-07-13T20:41:00+08:00", captured_at: "2026-07-14T00:20:00+08:00" },
    learning_goals: [] as string[],
    evidence_messages: [{ local_id: "e001", role: "user", observed_at: null, excerpt: "GPI 锚和 CD55 CD59", purpose: "knowledge_answer" }],
    concepts: [{ name: "阵发性睡眠性血红蛋白尿", preferred_english: "paroxysmal nocturnal hemoglobinuria", concept_type: "disease", scope_note: null, evidence_local_ids: ["e001"] }],
    claims: [{ statement: "PIGA 异常导致 GPI 锚缺失", claim_type: "mechanism", concept_terms: ["PNH", "PIGA"], evidence_local_ids: ["e001"], question_priority: "medium" }],
    learner_evidence: [{ concept_terms: ["PNH"], evidence_type: "correct_independent", confidence: 0.9, rationale: "用户独立回答", evidence_local_ids: ["e001"] }],
    misconceptions: [],
    unresolved_questions: [],
    unfinished_topics: [] as { title: string; evidence_local_ids: string[] }[],
  };
}

function mcp(method: string, params: Record<string, unknown> = {}, auth = workToken) {
  return request("/mcp", { method: "POST", headers: { authorization: `Bearer ${auth}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
}

async function digest(text: string) {
  const bytes = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((item) => item.toString(16).padStart(2, "0")).join("");
}

beforeEach(() => {
  bucket = new Bucket();
  env = {
    CONTROL_BUCKET: bucket as unknown as R2Bucket,
    MEDLEARN_INGEST_TOKEN: token,
    MEDLEARN_WORK_TOKEN: workToken,
    GITHUB_ACTIONS_DISPATCH_TOKEN: "github-secret",
  };
  dispatch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", dispatch);
});

afterEach(() => vi.unstubAllGlobals());

describe("shared contracts", () => {
  it("validates the shared golden envelope and JobRecord schemas", async () => {
    const ajv = new Ajv({ strict: false });
    addFormats(ajv);
    expect(ajv.compile(intakeSchema)(JSON.parse(fixtureText))).toBe(true);
    const response = await handle(capture(), env);
    expect(response.status).toBe(202);
    const validateJob = ajv.compile(jobSchema);
    const job = await response.json<JobRecord>();
    expect(validateJob(job)).toBe(true);
    expect(validateJob({ ...job, status: "succeeded" })).toBe(false);
    expect(validateJob({ ...job, status: "failed" })).toBe(false);
    expect(validateJob({
      ...job, status: "failed", error_code: "FAILED", dispatch_lease_id: "lease",
    })).toBe(false);
  });

  it("stores exact envelope bytes under the intake digest", async () => {
    const response = await handle(capture(), env);
    const job = await response.json<JobRecord>();
    const hex = await digest(fixtureText);
    expect(job.intake_digest).toBe(`sha256:${hex}`);
    expect(job.intake_object_key).toBe(`v1/intakes/sha256/${hex}.json`);
    expect(bucket.objects.get(job.intake_object_key)?.text).toBe(fixtureText);
    const [, init] = dispatch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).inputs).toEqual({
      job_id: job.job_id, intake_object_key: job.intake_object_key, intake_digest: job.intake_digest,
    });
  });

  it.each([
    [{ ...JSON.parse(fixtureText), intake_version: "0.2.0" }, "UNSUPPORTED_INTAKE_VERSION"],
    [{ ...JSON.parse(fixtureText), draft: { ...JSON.parse(fixtureText).draft, draft_version: "0.2.0" } }, "UNSUPPORTED_DRAFT_VERSION"],
  ])("rejects unsupported envelope versions", async (payload, code) => {
    const response = await handle(capture(JSON.stringify(payload), code), env);
    expect(response.status).toBe(422);
    expect(await response.json()).toEqual({ error: code });
  });
});

describe("baseline API", () => {
  it("keeps root and health public", async () => {
    expect((await handle(request("/"), env)).status).toBe(200);
    expect(await (await handle(request("/health"), env)).json()).toEqual({ status: "ok" });
  });

  it("returns one sanitized 401 shape for absent and wrong credentials", async () => {
    const missing = await handle(request("/v1/jobs/x"), env);
    const wrong = await handle(request("/v1/jobs/x", { headers: { authorization: "Bearer wrong" } }), env);
    expect([missing.status, wrong.status]).toEqual([401, 401]);
    expect(await missing.text()).toBe(await wrong.text());
  });

  it.each([
    ["text/plain", fixtureText, 415, "INVALID_CONTENT_TYPE"],
    ["application/json", "{", 400, "INVALID_JSON"],
    ["application/json", "x".repeat(1024 * 1024 + 1), 413, "BODY_TOO_LARGE"],
  ])("rejects invalid transport input", async (contentType, body, status, code) => {
    const input = request("/v1/captures", {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": contentType, "idempotency-key": "transport" },
      body,
    });
    const response = await handle(input, env);
    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ error: code });
  });

  it("retrieves jobs and rejects arbitrary object paths", async () => {
    const job = await (await handle(capture(), env)).json<JobRecord>();
    const auth = { authorization: `Bearer ${token}` };
    expect((await handle(request(`/v1/jobs/${job.job_id}`, { headers: auth }), env)).status).toBe(200);
    expect((await handle(request("/v1/jobs/..%2Fsecret", { headers: auth }), env)).status).toBe(400);
  });
});

describe("recovery and concurrency", () => {
  it("shares one job for duplicate and concurrent submissions", async () => {
    const [a, b] = await Promise.all([handle(capture(), env), handle(capture(), env)]);
    const [one, two] = await Promise.all([a.json<JobRecord>(), b.json<JobRecord>()]);
    expect(one.job_id).toBe(two.job_id);
    expect(dispatch).toHaveBeenCalledTimes(1);
  });

  it("returns 409 when one key is reused with another intake", async () => {
    await handle(capture(), env);
    const payload = JSON.parse(fixtureText);
    payload.client_kind = "ios_shortcut";
    const response = await handle(capture(JSON.stringify(payload)), env);
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "IDEMPOTENCY_CONFLICT" });
  });

  it("recovers after failure immediately following idempotency creation", async () => {
    bucket.failNextPrefix = "v1/intakes/";
    expect((await handle(capture(), env)).status).toBe(503);
    const claim = [...bucket.objects.entries()].find(([key]) => key.startsWith("v1/idempotency/"));
    expect(claim).toBeDefined();
    const jobId = JSON.parse(claim![1].text).job_id;
    const retried = await handle(capture(), env);
    expect((await retried.json<JobRecord>()).job_id).toBe(jobId);
    expect(dispatch).toHaveBeenCalledTimes(1);
  });

  it("repairs missing intake and job objects and safely resumes the handoff", async () => {
    const first = await (await handle(capture(), env)).json<JobRecord>();
    bucket.objects.delete(first.intake_object_key);
    bucket.objects.delete(`v1/jobs/${first.job_id}.json`);
    const repaired = await (await handle(capture(), env)).json<JobRecord>();
    expect(repaired.job_id).toBe(first.job_id);
    expect(bucket.objects.has(first.intake_object_key)).toBe(true);
    expect(bucket.objects.has(`v1/jobs/${first.job_id}.json`)).toBe(true);
    expect(dispatch).toHaveBeenCalledTimes(2);
  });

  it("retries an expired interrupted dispatch lease", async () => {
    const first = await (await handle(capture(), env)).json<JobRecord>();
    const key = `v1/jobs/${first.job_id}.json`;
    const interrupted: JobRecord = {
      ...first, status: "received", dispatch_lease_id: "old-lease",
      dispatch_lease_expires_at: "2000-01-01T00:00:00Z",
    };
    await bucket.put(key, JSON.stringify(interrupted));
    const retried = await (await handle(capture(), env)).json<JobRecord>();
    expect(retried.status).toBe("dispatched");
    expect(retried.dispatch_attempt).toBe(first.dispatch_attempt + 1);
    expect(dispatch).toHaveBeenCalledTimes(2);
  });

  it("retries a sanitized dispatch failure", async () => {
    dispatch.mockResolvedValueOnce(new Response("upstream medical data", { status: 500 }));
    const failedResponse = await handle(capture(), env);
    expect(failedResponse.status).toBe(502);
    expect(await failedResponse.text()).not.toContain("upstream medical data");
    const retried = await (await handle(capture(), env)).json<JobRecord>();
    expect(retried.status).toBe("dispatched");
    expect(dispatch).toHaveBeenCalledTimes(2);
  });

  it("rejects stale conditional job updates", async () => {
    const id = "stale-job";
    const key = `v1/jobs/${id}.json`;
    const received: JobRecord = {
      job_version: "0.2.0", job_id: id, status: "received", intake_digest: `sha256:${"a".repeat(64)}`,
      intake_object_key: `v1/intakes/sha256/${"a".repeat(64)}.json`, dispatch_attempt: 0,
      created_at: "2026-07-12T00:00:00Z", updated_at: "2026-07-12T00:00:00Z",
    };
    await bucket.put(key, JSON.stringify(received));
    const object = await bucket.get(key);
    const stale: Stored<JobRecord> = { value: received, etag: object!.etag };
    await bucket.put(key, JSON.stringify({ ...received, status: "dispatched" }));
    expect(await transitionJob(
      bucket as unknown as R2Bucket, key, stale,
      { ...received, status: "failed", error_code: "STALE" },
    )).toBe(false);
    expect(JSON.parse(bucket.objects.get(key)!.text).status).toBe("dispatched");
  });
});

describe("security boundary", () => {
  it.each([
    { CONTROL_BUCKET: bucket, GITHUB_ACTIONS_DISPATCH_TOKEN: "x" },
    { CONTROL_BUCKET: bucket, MEDLEARN_INGEST_TOKEN: "short", GITHUB_ACTIONS_DISPATCH_TOKEN: "x" },
    { CONTROL_BUCKET: bucket, MEDLEARN_INGEST_TOKEN: token },
    { MEDLEARN_INGEST_TOKEN: token, GITHUB_ACTIONS_DISPATCH_TOKEN: "x" },
  ])("fails closed for configuration case %#", async (badEnv) => {
    const response = await handle(capture(), badEnv as Env);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "SERVICE_MISCONFIGURED" });
  });

  it("sets no-store and authorization variance on every v1 response", async () => {
    for (const response of [
      await handle(request("/v1/jobs/x"), env),
      await handle(capture(), env),
      await handle(request("/v1/jobs/missing", { headers: { authorization: `Bearer ${token}` } }), env),
    ]) {
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("vary")).toBe("Authorization");
    }
  });

  it("does not log or return tokens or medical body text", async () => {
    const medical = "private COPD misconception";
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    bucket.failNextPrefix = "v1/intakes/";
    const response = await handle(capture(fixtureText.replace("COPD", medical), "sensitive"), env);
    const text = await response.text();
    expect(text).not.toContain(medical);
    expect(text).not.toContain(token);
    expect(text).not.toContain("github-secret");
    expect(log).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledWith(JSON.stringify({ stage: "v1_route", error_code: "CONTROL_STORAGE_FAILURE" }));
  });
});

describe("Chat Project Source MCP handoff", () => {
  it("exposes only submit_learning_handoff and validates the shared schema", async () => {
    const ajv = new Ajv({ strict: false });
    addFormats(ajv);
    expect(ajv.compile(handoffSchema)(handoff())).toBe(true);
    const response = await handle(mcp("tools/list"), env);
    const body = await response.json<{ result: { tools: { name: string; inputSchema: object }[] } }>();
    expect(body.result.tools.map((tool) => tool.name)).toEqual(["submit_learning_handoff"]);
    expect(ajv.compile(body.result.tools[0].inputSchema)( { handoff: handoff() } )).toBe(true);
  });

  it("submits deterministic bytes with one job and a stable result", async () => {
    const args = { name: "submit_learning_handoff", arguments: { handoff: handoff() } };
    const first = await handle(mcp("tools/call", args), env);
    const second = await handle(mcp("tools/call", args), env);
    expect(first.status).toBe(200);
    const a = await first.json<{ result: { structuredContent: { job_id: string; intake_digest: string } } }>();
    const b = await second.json<{ result: { structuredContent: { job_id: string; intake_digest: string } } }>();
    expect(a.result.structuredContent).toEqual(b.result.structuredContent);
    expect(a.result.structuredContent.intake_digest).toMatch(/^sha256:[a-f0-9]{64}$/);
    const stored = [...bucket.objects.entries()].find(([key]) => key.startsWith("v1/intakes/"));
    expect(stored).toBeDefined();
    const draft = JSON.parse(stored![1].text).draft;
    expect(draft.context.source_id).toMatch(/^source_[a-f0-9]{32}$/);
    expect(draft.evidence_messages[0].observed_at).toBe("2026-07-14T00:20:00+08:00");
  });

  it("fails rather than dropping unmappable learning goals or unfinished topics", async () => {
    for (const update of [
      (value: ReturnType<typeof handoff>) => { value.learning_goals = ["理解 PNH"]; },
      (value: ReturnType<typeof handoff>) => { value.unfinished_topics = [{ title: "流式", evidence_local_ids: ["e001"] }]; },
    ]) {
      const value = handoff();
      update(value);
      const response = await handle(mcp("tools/call", {
        name: "submit_learning_handoff", arguments: { handoff: value },
      }), env);
      expect((await response.json<{ error: { message: string } }>()).error.message)
        .toBe("HANDOFF_CONVERSION_FAILURE");
    }
  });

  it.each([
    ["duplicate", (value: ReturnType<typeof handoff>) => { value.evidence_messages.push({ ...value.evidence_messages[0] }); }, "HANDOFF_DUPLICATE_LOCAL_ID"],
    ["dangling", (value: ReturnType<typeof handoff>) => { value.claims[0].evidence_local_ids = ["missing"]; }, "HANDOFF_DANGLING_EVIDENCE_REFERENCE"],
    ["unsupported", (value: ReturnType<typeof handoff>) => { value.handoff_version = "9.9.9"; }, "HANDOFF_UNSUPPORTED_VERSION"],
  ])("returns a stable error for %s input", async (_name, mutate, code) => {
    const value = handoff();
    mutate(value);
    const response = await handle(mcp("tools/call", { name: "submit_learning_handoff", arguments: { handoff: value } }), env);
    const body = await response.json<{ error: { message: string } }>();
    expect(body.error.message).toBe(code);
  });

  it("requires the dedicated work secret and never exposes it", async () => {
    const response = await handle(mcp("tools/list", {}, token), env);
    const text = await response.text();
    expect(response.status).toBe(401);
    expect(text).not.toContain(workToken);
    expect(text).not.toContain(token);
  });

  it("rejects an unrecognized tool and the work token on the ingest route", async () => {
    const unknown = await handle(mcp("tools/call", { name: "read_project_sources", arguments: {} }), env);
    expect((await unknown.json<{ error: { message: string } }>()).error.message).toBe("HANDOFF_SCHEMA_INVALID");
    expect((await handle(request("/v1/jobs/x", { headers: { authorization: `Bearer ${workToken}` } }), env)).status).toBe(401);
  });

  it("does not persist the work token and does not require it for health or intake", async () => {
    const response = await handle(mcp("tools/call", {
      name: "submit_learning_handoff", arguments: { handoff: handoff() },
    }), env);
    expect((await response.text())).not.toContain(workToken);
    expect([...bucket.objects.values()].map((item) => item.text).join("\n")).not.toContain(workToken);
    const noWork = { ...env, MEDLEARN_WORK_TOKEN: undefined };
    expect((await handle(request("/health"), noWork)).status).toBe(200);
    expect((await handle(capture(fixtureText, "no-work"), noWork)).status).toBe(202);
  });

  it("rejects an oversized MCP body before parsing it", async () => {
    const response = await handle(request("/mcp", {
      method: "POST", headers: { authorization: `Bearer ${workToken}`, "content-type": "application/json" },
      body: "x".repeat(256 * 1024 + 1),
    }), env);
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "HANDOFF_SCHEMA_INVALID" });
  });
});

// ── vault read API ─────────────────────────────────────────────────

function vaultAuth(token: string) {
  return { authorization: `Bearer ${token}` };
}

function canonicalReceipt(overrides: Record<string, unknown> = {}) {
  const receipt = {
    receipt_version: "0.1.0",
    publication_plan_id: "publication_plan_00000000000000000000000000000001",
    publication_plan_object_digest: "sha256:" + "a".repeat(64),
    capture_id: "capture_" + "b".repeat(32),
    artifacts: [
      {
        path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json",
        media_type: "application/json; charset=utf-8",
        content_digest: "sha256:" + "c".repeat(64),
        byte_length: 100,
      },
      {
        path: "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md",
        media_type: "text/markdown; charset=utf-8",
        content_digest: "sha256:" + "d".repeat(64),
        byte_length: 200,
      },
    ],
    ...overrides,
  };
  // Canonical JSON: sorted keys, compact separators, one LF
  return JSON.stringify(receipt, function replacer(_key, value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const sorted: Record<string, unknown> = {};
      for (const k of Object.keys(value).sort()) {
        sorted[k] = (value as Record<string, unknown>)[k];
      }
      return sorted;
    }
    return value;
  }) + "\n";
}

function receiptKey(planId = "publication_plan_00000000000000000000000000000001") {
  return `v1/publications/${planId}.json`;
}

function setupVaultEnv(syncToken = "sync-secret-that-is-at-least-32-bytes") {
  const vaultBucket = new Bucket();
  return {
    vaultBucket,
    env: {
      CONTROL_BUCKET: bucket as unknown as R2Bucket,
      VAULT_BUCKET: vaultBucket as unknown as R2Bucket,
      MEDLEARN_INGEST_TOKEN: token,
      MEDLEARN_SYNC_TOKEN: syncToken,
      GITHUB_ACTIONS_DISPATCH_TOKEN: "github-secret",
    } as Env,
  };
}

describe("vault read API", () => {
  const syncToken = "sync-secret-that-is-at-least-32-bytes";

  // ── auth ──────────────────────────────────────────────────────────

  it("returns 401 when sync token is missing", async () => {
    const { env: vaultEnv } = setupVaultEnv();
    const response = await handle(request("/v1/vault/manifest"), vaultEnv);
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "UNAUTHORIZED" });
    expect(response.headers.get("vary")).toBe("Authorization");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("returns 401 for wrong sync token", async () => {
    const { env: vaultEnv } = setupVaultEnv();
    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth("wrong-token-that-is-at-least-32") }),
      vaultEnv,
    );
    expect(response.status).toBe(401);
  });

  it("sets no-store cache headers on unknown vault endpoints", async () => {
    const { env: vaultEnv } = setupVaultEnv();
    const response = await handle(request("/v1/vault/unknown", { headers: vaultAuth(syncToken) }), vaultEnv);
    expect(response.status).toBe(404);
    expect(response.headers.get("vary")).toBe("Authorization");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it.each([token, workToken])("rejects non-sync token for vault route", async (otherToken) => {
    const { env: vaultEnv } = setupVaultEnv();
    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(otherToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(401);
  });

  it("rejects sync token for capture intake", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    // ensure control config is valid
    vaultEnv.MEDLEARN_INGEST_TOKEN = token;
    vaultEnv.CONTROL_BUCKET = vaultBucket as unknown as R2Bucket;
    const response = await handle(
      request("/v1/captures", {
        method: "POST",
        headers: {
          authorization: `Bearer ${syncToken}`,
          "content-type": "application/json",
          "idempotency-key": "vault-test",
        },
        body: fixtureText,
      }),
      vaultEnv,
    );
    expect(response.status).toBe(401);
  });

  // ── configuration ─────────────────────────────────────────────────

  it("returns 503 for vault route when vault bucket is missing", async () => {
    const envNoVault: Env = {
      CONTROL_BUCKET: bucket as unknown as R2Bucket,
      MEDLEARN_INGEST_TOKEN: token,
      MEDLEARN_SYNC_TOKEN: syncToken,
      GITHUB_ACTIONS_DISPATCH_TOKEN: "github-secret",
    };
    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      envNoVault,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_SERVICE_MISCONFIGURED" });
  });

  it("keeps /health working when vault binding is missing", async () => {
    const envNoVault: Env = {
      CONTROL_BUCKET: bucket as unknown as R2Bucket,
      MEDLEARN_INGEST_TOKEN: token,
      GITHUB_ACTIONS_DISPATCH_TOKEN: "github-secret",
    };
    const response = await handle(request("/health"), envNoVault);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  it("keeps intake route working when vault binding is missing", async () => {
    const envNoVault: Env = {
      CONTROL_BUCKET: bucket as unknown as R2Bucket,
      MEDLEARN_INGEST_TOKEN: token,
      GITHUB_ACTIONS_DISPATCH_TOKEN: "github-secret",
    };
    const response = await handle(capture(), envNoVault);
    expect(response.status).toBe(202);
  });

  // ── manifest: empty ───────────────────────────────────────────────

  it("returns empty manifest for empty receipt set", async () => {
    const { env: vaultEnv } = setupVaultEnv();
    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    const body = await response.json<{ manifest_version: string; artifacts: unknown[] }>();
    expect(body.manifest_version).toBe("0.1.0");
    expect(body.artifacts).toEqual([]);
  });

  // ── manifest: single receipt ──────────────────────────────────────

  it("returns two artifacts from a single receipt", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const receipt = canonicalReceipt();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    const body = await response.json<{ artifacts: { path: string }[] }>();
    expect(body.artifacts.length).toBe(2);
    expect(response.headers.get("cache-control")).toBe("private, no-cache");
  });

  it("reads each receipt body exactly once", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const key = receiptKey();
    vaultBucket.seed(key, canonicalReceipt());
    expect((await handle(request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }), vaultEnv)).status).toBe(200);
    expect(vaultBucket.bodyConsumes.get(key)).toBe(1);
  });

  // ── manifest: deterministic ordering ──────────────────────────────

  it("sorts artifacts by path ascending across multiple receipts", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();

    // Receipt 2 has a lexicographically earlier path
    const r1 = canonicalReceipt();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", r1);

    const r2 = canonicalReceipt({
      publication_plan_id: "publication_plan_00000000000000000000000000000002",
      capture_id: "capture_" + "a".repeat(32),
      artifacts: [
        { path: "MedLearn/Data/Captures/capture_" + "a".repeat(32) + ".json", media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "f".repeat(64), byte_length: 25 },
        { path: "MedLearn/Captures/2026/06/capture_" + "a".repeat(32) + ".md", media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + "e".repeat(64), byte_length: 50 },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000002.json", r2);

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    const body = await response.json<{ artifacts: { path: string }[] }>();
    const paths = body.artifacts.map(a => a.path);
    // Must be sorted ascending
    expect(paths).toEqual([...paths].sort());
  });

  // ── manifest: duplicate dedup ─────────────────────────────────────

  it("deduplicates identical artifacts from different receipts", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const r1 = canonicalReceipt();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", r1);
    // Same receipt in a different plan key → same artifacts
    const r2 = canonicalReceipt({
      publication_plan_id: "publication_plan_00000000000000000000000000000002",
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000002.json", r2);

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    const body = await response.json<{ artifacts: unknown[] }>();
    expect(body.artifacts.length).toBe(2);
  });

  // ── manifest: conflicts ───────────────────────────────────────────

  it("returns VAULT_MANIFEST_CONFLICT for conflicting duplicates", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", canonicalReceipt());

    // Different digest for same path
    const conflict = canonicalReceipt({
      publication_plan_id: "publication_plan_00000000000000000000000000000002",
      artifacts: [
        {
          path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json",
          media_type: "application/json; charset=utf-8",
          content_digest: "sha256:" + "0".repeat(64),
          byte_length: 100,
        },
        {
          path: "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md",
          media_type: "text/markdown; charset=utf-8",
          content_digest: "sha256:" + "d".repeat(64),
          byte_length: 200,
        },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000002.json", conflict);

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_MANIFEST_CONFLICT" });
  });

  it("propagates manifest conflicts on download without reading the artifact", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const path = `MedLearn/Captures/2026/07/capture_${"b".repeat(32)}.md`;
    vaultBucket.seed(receiptKey(), canonicalReceipt());
    vaultBucket.seed(receiptKey("publication_plan_00000000000000000000000000000002"), canonicalReceipt({
      publication_plan_id: "publication_plan_00000000000000000000000000000002",
      artifacts: [
        JSON.parse(canonicalReceipt()).artifacts[0],
        { ...JSON.parse(canonicalReceipt()).artifacts[1], content_digest: "sha256:" + "0".repeat(64) },
      ],
    }));
    const response = await handle(request(`/v1/vault/files?path=${encodeURIComponent(path)}`, { headers: vaultAuth(syncToken) }), vaultEnv);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_MANIFEST_CONFLICT" });
    expect(vaultBucket.getCalls.get(path) ?? 0).toBe(0);
  });

  // ── manifest: malformed receipt ───────────────────────────────────

  it("fails closed on malformed receipt", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", '{not valid json}');

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "INVALID_VAULT_PUBLICATION_RECEIPT" });
  });

  it("fails closed on non-canonical receipt", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    // JSON with indentation (not canonical)
    const nonCanonical = JSON.stringify(JSON.parse(canonicalReceipt()), null, 2) + "\n";
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", nonCanonical);

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "INVALID_VAULT_PUBLICATION_RECEIPT" });
  });

  it("fails closed on invalid receipt schema", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const invalid = canonicalReceipt({ receipt_version: "99.0.0" });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", invalid);

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "INVALID_VAULT_PUBLICATION_RECEIPT" });
  });

  it.each([
    ["top-level extra field", { extra: true }],
    ["artifact extra field", { artifacts: [{ ...JSON.parse(canonicalReceipt()).artifacts[0], extra: true }, JSON.parse(canonicalReceipt()).artifacts[1]] }],
    ["fractional byte length", { artifacts: [{ ...JSON.parse(canonicalReceipt()).artifacts[0], byte_length: 1.5 }, JSON.parse(canonicalReceipt()).artifacts[1]] }],
    ["missing required field", { artifacts: [{ ...JSON.parse(canonicalReceipt()).artifacts[0], byte_length: undefined }, JSON.parse(canonicalReceipt()).artifacts[1]] }],
  ])("rejects schema-invalid receipt: %s", async (_name, overrides) => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed(receiptKey(), canonicalReceipt(overrides));
    const response = await handle(request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }), vaultEnv);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "INVALID_VAULT_PUBLICATION_RECEIPT" });
  });

  it.each([
    ["non-json receipt key", "v1/publications/not-a-receipt.txt", canonicalReceipt(), undefined],
    ["key/body plan mismatch", receiptKey("publication_plan_00000000000000000000000000000002"), canonicalReceipt(), undefined],
    ["wrong receipt Content-Type", receiptKey(), canonicalReceipt(), "application/json"],
  ])("fails closed on %s", async (_name, key, receipt, contentType) => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed(key, receipt, contentType);
    const response = await handle(request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }), vaultEnv);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "INVALID_VAULT_PUBLICATION_RECEIPT" });
  });

  it("returns storage error when a listed receipt disappears before get", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.listedMissing.add(receiptKey());
    const response = await handle(request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }), vaultEnv);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_STORAGE_UNAVAILABLE" });
  });

  it.each([
    ["JSON capture mismatch", { artifacts: [{ ...JSON.parse(canonicalReceipt()).artifacts[0], path: `MedLearn/Data/Captures/capture_${"a".repeat(32)}.json` }, JSON.parse(canonicalReceipt()).artifacts[1]] }],
    ["Markdown capture mismatch", { artifacts: [JSON.parse(canonicalReceipt()).artifacts[0], { ...JSON.parse(canonicalReceipt()).artifacts[1], path: `MedLearn/Captures/2026/07/capture_${"a".repeat(32)}.md` }] }],
    ["Markdown month 13", { artifacts: [JSON.parse(canonicalReceipt()).artifacts[0], { ...JSON.parse(canonicalReceipt()).artifacts[1], path: `MedLearn/Captures/2026/13/capture_${"b".repeat(32)}.md` }] }],
    ["artifact traversal", { artifacts: [{ ...JSON.parse(canonicalReceipt()).artifacts[0], path: "MedLearn/Data/Captures/../capture_bad.json" }, JSON.parse(canonicalReceipt()).artifacts[1]] }],
  ])("rejects semantic artifact path: %s", async (_name, overrides) => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed(receiptKey(), canonicalReceipt(overrides));
    const response = await handle(request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }), vaultEnv);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "INVALID_VAULT_PUBLICATION_RECEIPT" });
  });

  it("uses deterministic duplicate provenance regardless of receipt order", () => {
    const first = JSON.parse(canonicalReceipt());
    const second = JSON.parse(canonicalReceipt({ publication_plan_id: "publication_plan_00000000000000000000000000000002" }));
    const a = buildManifest([second, first]);
    const b = buildManifest([first, second]);
    expect(JSON.stringify(a.manifest)).toBe(JSON.stringify(b.manifest));
    expect(a.manifest.artifacts.every((artifact) => artifact.publication_plan_id === first.publication_plan_id)).toBe(true);
  });

  // ── manifest: ETag ────────────────────────────────────────────────

  it("returns stable ETag for manifest", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", canonicalReceipt());

    const r1 = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    const r2 = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(r1.headers.get("etag")).toBe(r2.headers.get("etag"));
    expect(r1.headers.get("etag")).toMatch(/^"sha256:[a-f0-9]{64}"$/);
  });

  it("returns 304 for matching If-None-Match on manifest", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", canonicalReceipt());

    const r1 = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    const etag = r1.headers.get("etag")!;

    const r2 = await handle(
      request("/v1/vault/manifest", {
        headers: { ...vaultAuth(syncToken), "if-none-match": etag },
      }),
      vaultEnv,
    );
    expect(r2.status).toBe(304);
    expect(await r2.text()).toBe("");
  });

  // ── file download ─────────────────────────────────────────────────

  it("downloads exact artifact bytes", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const receipt = canonicalReceipt();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);

    const mdContent = "# Test Markdown\n";
    const mdDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mdContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");

    const receiptWithMd = canonicalReceipt({
      artifacts: [
        {
          path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json",
          media_type: "application/json; charset=utf-8",
          content_digest: "sha256:" + "c".repeat(64),
          byte_length: 100,
        },
        {
          path: "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md",
          media_type: "text/markdown; charset=utf-8",
          content_digest: "sha256:" + mdDigestHex,
          byte_length: mdContent.length,
        },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receiptWithMd);
    vaultBucket.seed(
      "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md",
      mdContent,
      "text/markdown; charset=utf-8",
    );

    const response = await handle(
      request(`/v1/vault/files?path=MedLearn%2FCaptures%2F2026%2F07%2Fcapture_${"b".repeat(32)}.md`, {
        headers: vaultAuth(syncToken),
      }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toBe(mdContent);
    expect(response.headers.get("content-type")).toBe("text/markdown; charset=utf-8");
    expect(response.headers.get("cache-control")).toBe("private, no-cache");
  });

  it("reads the receipt and artifact bodies exactly once during download", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const receiptPath = receiptKey();
    const artifactPath = `MedLearn/Captures/2026/07/capture_${"b".repeat(32)}.md`;
    const artifact = "# once\n";
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(artifact)))]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
    vaultBucket.seed(receiptPath, canonicalReceipt({
      artifacts: [
        JSON.parse(canonicalReceipt()).artifacts[0],
        { ...JSON.parse(canonicalReceipt()).artifacts[1], content_digest: `sha256:${digest}`, byte_length: artifact.length },
      ],
    }));
    vaultBucket.seed(artifactPath, artifact, "text/markdown; charset=utf-8");
    expect((await handle(request(`/v1/vault/files?path=${encodeURIComponent(artifactPath)}`, { headers: vaultAuth(syncToken) }), vaultEnv)).status).toBe(200);
    expect(vaultBucket.bodyConsumes.get(receiptPath)).toBe(1);
    expect(vaultBucket.bodyConsumes.get(artifactPath)).toBe(1);
  });

  it("returns correct Content-Type for Markdown artifact", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdContent = "# test\n";
    const mdDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mdContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";

    const receipt = canonicalReceipt({
      artifacts: [
        { path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json", media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "c".repeat(64), byte_length: 100 },
        { path: mdPath, media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + mdDigestHex, byte_length: mdContent.length },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(mdPath, mdContent, "text/markdown; charset=utf-8");

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/markdown; charset=utf-8");
  });

  it("returns correct Content-Type for JSON artifact", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const jsonContent = '{"test":true}\n';
    const jsonDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(jsonContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");
    const jsonPath = "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json";

    const receipt = canonicalReceipt({
      artifacts: [
        { path: jsonPath, media_type: "application/json; charset=utf-8", content_digest: "sha256:" + jsonDigestHex, byte_length: jsonContent.length },
        { path: "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md", media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + "d".repeat(64), byte_length: 200 },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(jsonPath, jsonContent, "application/json; charset=utf-8");

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(jsonPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json; charset=utf-8");
  });

  it("returns correct ETag for artifact download", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdContent = "# etag test\n";
    const mdDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mdContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";

    const receipt = canonicalReceipt({
      artifacts: [
        { path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json", media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "c".repeat(64), byte_length: 100 },
        { path: mdPath, media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + mdDigestHex, byte_length: mdContent.length },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(mdPath, mdContent, "text/markdown; charset=utf-8");

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.headers.get("etag")).toBe(`"sha256:${mdDigestHex}"`);
  });

  it("returns 304 for matching If-None-Match on artifact", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdContent = "# 304 test\n";
    const mdDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mdContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";

    const receipt = canonicalReceipt({
      artifacts: [
        { path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json", media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "c".repeat(64), byte_length: 100 },
        { path: mdPath, media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + mdDigestHex, byte_length: mdContent.length },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(mdPath, mdContent, "text/markdown; charset=utf-8");

    const r1 = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    const etag = r1.headers.get("etag")!;

    const r2 = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, {
        headers: { ...vaultAuth(syncToken), "if-none-match": etag },
      }),
      vaultEnv,
    );
    expect(r2.status).toBe(304);
  });

  // ── path validation ───────────────────────────────────────────────

  it.each([
    ["", "INVALID_VAULT_PATH"],
    ["..%2Fsecret", "INVALID_VAULT_PATH"],
    ["MedLearn%5Csecret", "INVALID_VAULT_PATH"],
    ["MedLearn//secret", "INVALID_VAULT_PATH"],
    ["notmedlearn/file.txt", "INVALID_VAULT_PATH"],
  ])("rejects invalid path: %s", async (badPath, errorCode) => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", canonicalReceipt());

    // Use the path as-is (not double-encoded)
    const response = await handle(
      request(`/v1/vault/files?path=${badPath}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: errorCode });
  });

  it("returns 404 for valid path not in manifest", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", canonicalReceipt());

    const response = await handle(
      request("/v1/vault/files?path=MedLearn%2FNotExists%2Ffile.md", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "NOT_FOUND" });
  });

  // ── integrity failures ────────────────────────────────────────────

  it("returns integrity failure when R2 object is missing", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";
    const receipt = canonicalReceipt();
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    // Don't seed the actual file

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" });
  });

  it("returns integrity failure when digest mismatches", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdContent = "# wrong digest\n";
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";

    const receipt = canonicalReceipt(); // uses fake digest "d"*64
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(mdPath, mdContent, "text/markdown; charset=utf-8");

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" });
  });

  it("returns integrity failure when byte length mismatches", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdContent = "# wrong length\n";
    const mdDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mdContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";

    const receipt = canonicalReceipt({
      artifacts: [
        { path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json", media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "c".repeat(64), byte_length: 100 },
        { path: mdPath, media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + mdDigestHex, byte_length: 99999 }, // wrong!
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(mdPath, mdContent, "text/markdown; charset=utf-8");

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" });
  });

  it("returns integrity failure when Content-Type mismatches", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    const mdContent = "# wrong content type\n";
    const mdDigestHex = [...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mdContent))
    )].map(b => b.toString(16).padStart(2, "0")).join("");
    const mdPath = "MedLearn/Captures/2026/07/capture_" + "b".repeat(32) + ".md";

    const receipt = canonicalReceipt({
      artifacts: [
        { path: "MedLearn/Data/Captures/capture_" + "b".repeat(32) + ".json", media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "c".repeat(64), byte_length: 100 },
        { path: mdPath, media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + mdDigestHex, byte_length: mdContent.length },
      ],
    });
    vaultBucket.seed("v1/publications/publication_plan_00000000000000000000000000000001.json", receipt);
    vaultBucket.seed(mdPath, mdContent, "text/plain"); // wrong content type!

    const response = await handle(
      request(`/v1/vault/files?path=${encodeURIComponent(mdPath)}`, { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "VAULT_ARTIFACT_INTEGRITY_FAILURE" });
  });

  // ── security: no leakage ───────────────────────────────────────────

  it("does not leak internal keys or tokens in error responses", async () => {
    const { env: vaultEnv } = setupVaultEnv();
    const response = await handle(
      request("/v1/vault/manifest"),
      vaultEnv,
    );
    const text = await response.text();
    expect(text).not.toContain(syncToken);
    expect(text).not.toContain("VAULT_BUCKET");
    expect(text).not.toContain("medlearn-vault");
    expect(text).not.toContain("Authorization");
  });

  // ── pagination ─────────────────────────────────────────────────────

  it("handles R2 multi-page pagination", async () => {
    const { vaultBucket, env: vaultEnv } = setupVaultEnv();
    // Seed 5 receipts → with page size 3, this exercises pagination
    for (let i = 0; i < 5; i++) {
      const planId = `publication_plan_${String(i).padStart(32, "0")}`;
      const capId = `capture_${String(i).padStart(32, "0")}`;
      const receipt = canonicalReceipt({
        publication_plan_id: planId,
        capture_id: capId,
        artifacts: [
          { path: `MedLearn/Data/Captures/${capId}.json`, media_type: "application/json; charset=utf-8", content_digest: "sha256:" + "c".repeat(64), byte_length: 100 },
          { path: `MedLearn/Captures/2026/07/${capId}.md`, media_type: "text/markdown; charset=utf-8", content_digest: "sha256:" + "d".repeat(64), byte_length: 200 },
        ],
      });
      vaultBucket.seed(`v1/publications/${planId}.json`, receipt);
    }

    const response = await handle(
      request("/v1/vault/manifest", { headers: vaultAuth(syncToken) }),
      vaultEnv,
    );
    expect(response.status).toBe(200);
    const body = await response.json<{ artifacts: { path: string }[] }>();
    // 5 receipts × 2 artifacts each = 10 unique paths
    expect(body.artifacts.length).toBe(10);
    // Sorted by path ascending
    const paths = body.artifacts.map(a => a.path);
    expect(paths).toEqual([...paths].sort());
  });
});
