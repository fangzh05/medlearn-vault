import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { handle, type Env, type JobRecord } from "../src/index";

class Bucket {
  objects = new Map<string, string>();
  async get(key: string) {
    const value = this.objects.get(key);
    return value === undefined ? null : { json: async <T>() => JSON.parse(value) as T };
  }
  async put(key: string, value: string | ArrayBuffer, options?: { onlyIf?: { etagDoesNotMatch?: string } }) {
    if (options?.onlyIf?.etagDoesNotMatch === "*" && this.objects.has(key)) return null;
    const text = typeof value === "string" ? value : new TextDecoder().decode(value);
    this.objects.set(key, text);
    return { key };
  }
}

const token = "ingest-secret";
let bucket: Bucket;
let env: Env;
let dispatch: ReturnType<typeof vi.fn>;

const envelope = {
  client_kind: "manual",
  draft: { draft_version: "0.3.0", context: {}, evidence_messages: [], concept_mentions: [], claim_candidates: [], misconception_candidates: [] },
};

function request(path: string, init: RequestInit = {}) {
  return new Request(`https://example.test${path}`, init);
}

function capture(body: unknown = envelope, key = "key-1", extra: HeadersInit = {}) {
  return request("/v1/captures", {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", "idempotency-key": key, ...extra },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

beforeEach(() => {
  bucket = new Bucket();
  env = { CONTROL_BUCKET: bucket as unknown as R2Bucket, MEDLEARN_INGEST_TOKEN: token, GITHUB_ACTIONS_DISPATCH_TOKEN: "github-secret" };
  dispatch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", dispatch);
});

afterEach(() => vi.unstubAllGlobals());

describe("public and authentication", () => {
  it("serves public root and health", async () => {
    expect((await handle(request("/"), env)).status).toBe(200);
    expect(await (await handle(request("/health"), env)).json()).toEqual({ status: "ok" });
  });

  it("returns the same sanitized 401 for missing and wrong tokens", async () => {
    const missing = await handle(request("/v1/jobs/x"), env);
    const wrong = await handle(request("/v1/jobs/x", { headers: { authorization: "Bearer wrong" } }), env);
    expect([missing.status, wrong.status]).toEqual([401, 401]);
    expect(await missing.text()).toBe(await wrong.text());
  });
});

describe("capture intake", () => {
  it("accepts a valid authenticated capture and fixes the dispatch target", async () => {
    const response = await handle(capture({ ...envelope, repository: "evil/repo", ref: "evil", workflow: "evil.yml" }), env);
    expect(response.status).toBe(202);
    const job = await response.json<JobRecord>();
    expect(job.status).toBe("dispatched");
    expect(job.job_version).toBe("0.1.0");
    expect([...bucket.objects.keys()]).toEqual(expect.arrayContaining([
      expect.stringMatching(/^v1\/drafts\/sha256\/[a-f0-9]{64}\.json$/),
      `v1/jobs/${job.job_id}.json`,
      expect.stringMatching(/^v1\/idempotency\/[a-f0-9]{64}\.json$/),
    ]));
    const [url, init] = dispatch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://api.github.com/repos/fangzh05/medlearn-vault/actions/workflows/medlearn-propose.yml/dispatches");
    const payload = JSON.parse(init.body as string);
    expect(payload).toEqual({ ref: "main", inputs: { job_id: job.job_id, draft_object_key: job.draft_object_key, draft_digest: job.draft_digest } });
  });

  it.each([
    [capture(envelope, "x", { "content-type": "text/plain" }), 415, "INVALID_CONTENT_TYPE"],
    [capture("{", "x"), 400, "INVALID_JSON"],
    [capture({ ...envelope, draft: { draft_version: "0.2.0" } }, "x"), 422, "UNSUPPORTED_DRAFT_VERSION"],
  ])("rejects invalid transport input", async (input, status, code) => {
    const response = await handle(input, env);
    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ error: code });
  });

  it("rejects an oversized body", async () => {
    const response = await handle(capture("x".repeat(1024 * 1024 + 1)), env);
    expect(response.status).toBe(413);
  });

  it("reuses one logical job for duplicate and concurrent submissions", async () => {
    const [a, b] = await Promise.all([handle(capture(), env), handle(capture(), env)]);
    const [one, two] = await Promise.all([a.json<JobRecord>(), b.json<JobRecord>()]);
    expect(one.job_id).toBe(two.job_id);
    expect(dispatch).toHaveBeenCalledTimes(1);
    const again = await (await handle(capture(), env)).json<JobRecord>();
    expect(again.job_id).toBe(one.job_id);
  });

  it("returns conflict for the same key with a different digest", async () => {
    await handle(capture(), env);
    const changed = { ...envelope, client_kind: "ios_shortcut" };
    const response = await handle(capture(changed), env);
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "IDEMPOTENCY_CONFLICT" });
  });

  it("stores only a sanitized failed job when dispatch fails", async () => {
    const medical = "secret medical text";
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    dispatch.mockResolvedValue(new Response(medical, { status: 500 }));
    const response = await handle(capture({ ...envelope, draft: { ...envelope.draft, note: medical } }), env);
    expect(response.status).toBe(502);
    const job = await response.json<JobRecord>();
    expect(job).toMatchObject({ status: "failed", error_code: "GITHUB_DISPATCH_FAILED" });
    expect(JSON.stringify(job)).not.toContain(medical);
    expect(JSON.stringify(job)).not.toContain(token);
    expect(JSON.stringify(job)).not.toContain("github-secret");
    expect(log).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });

  it("sanitizes a dispatch network failure", async () => {
    dispatch.mockRejectedValue(new Error("socket included sensitive upstream details"));
    const response = await handle(capture(envelope, "network-failure"), env);
    expect(response.status).toBe(502);
    expect(await response.json()).toMatchObject({ status: "failed", error_code: "GITHUB_DISPATCH_FAILED" });
  });
});

describe("retrieval", () => {
  it("retrieves jobs and proposals and returns 404 for missing objects", async () => {
    const created = await (await handle(capture(), env)).json<JobRecord>();
    const auth = { authorization: `Bearer ${token}` };
    expect((await handle(request(`/v1/jobs/${created.job_id}`, { headers: auth }), env)).status).toBe(200);
    const proposalId = `proposal_${"a".repeat(32)}`;
    await bucket.put(`v1/proposals/${proposalId}.json`, JSON.stringify({ proposal_id: proposalId }));
    expect(await (await handle(request(`/v1/proposals/${proposalId}`, { headers: auth }), env)).json()).toEqual({ proposal_id: proposalId });
    expect((await handle(request(`/v1/jobs/missing`, { headers: auth }), env)).status).toBe(404);
  });

  it("sanitizes retrieved job records", async () => {
    const id = "job-with-extra";
    await bucket.put(`v1/jobs/${id}.json`, JSON.stringify({
      job_version: "0.1.0", job_id: id, status: "failed", draft_digest: `sha256:${"a".repeat(64)}`,
      draft_object_key: `v1/drafts/sha256/${"a".repeat(64)}.json`, created_at: "2026-07-11T00:00:00Z",
      updated_at: "2026-07-11T00:00:00Z", error_code: "FAILED", medical_text: "must not escape",
    }));
    const response = await handle(request(`/v1/jobs/${id}`, { headers: { authorization: `Bearer ${token}` } }), env);
    expect(await response.text()).not.toContain("medical_text");
  });

  it.each(["../secret", "%2e%2e%2fsecret", "bad.id"])("rejects invalid and traversal identifiers", async (id) => {
    const response = await handle(request(`/v1/jobs/${id}`, { headers: { authorization: `Bearer ${token}` } }), env);
    expect([400, 404]).toContain(response.status);
    expect([...bucket.objects.keys()].some((key) => key.includes("secret"))).toBe(false);
  });
});
