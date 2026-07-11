import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import jobSchema from "../contracts/job-record.schema.json";
import { handle, transitionJob, type Env, type JobRecord, type Stored } from "../src/index";
import intakeSchema from "../../schemas/workflow/current/intake_envelope.schema.json";

class Bucket {
  objects = new Map<string, { text: string; etag: string }>();
  failNextPrefix?: string;
  private revision = 0;

  async get(key: string) {
    const object = this.objects.get(key);
    return object === undefined ? null : {
      etag: object.etag,
      httpEtag: `"${object.etag}"`,
      json: async <T>() => JSON.parse(object.text) as T,
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
}

const token = "ingest-secret-that-is-at-least-32-bytes";
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
    expect(ajv.compile(jobSchema)(await response.json())).toBe(true);
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
