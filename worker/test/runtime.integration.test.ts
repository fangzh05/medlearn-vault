import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Miniflare } from "miniflare";
import { MockAgent } from "undici";

let runtime: Miniflare;
const token = "runtime-ingest-secret-that-is-at-least-32-bytes";
const runtimeSyncToken = "runtime-sync-secret-that-is-at-least-32-bytes";
const dispatchMock = new MockAgent();
const github = dispatchMock.get("https://api.github.com");

beforeAll(() => {
  dispatchMock.disableNetConnect();
  github
    .intercept({
      method: "POST",
      path: "/repos/fangzh05/medlearn-vault/actions/workflows/medlearn-propose.yml/dispatches",
    })
    .reply(204)
    .persist();
  runtime = new Miniflare({
    modules: true,
    scriptPath: ".runtime-test/index.js",
    r2Buckets: ["CONTROL_BUCKET", "VAULT_BUCKET"],
    bindings: {
      MEDLEARN_INGEST_TOKEN: token,
      MEDLEARN_SYNC_TOKEN: runtimeSyncToken,
      MEDLEARN_WORK_DISPATCH_MODE: "github",
      GITHUB_ACTIONS_DISPATCH_TOKEN: "runtime-github-token",
    },
    fetchMock: dispatchMock,
  });
});

function canonicalJson(value: unknown): string {
  return JSON.stringify(value, function replacer(_key, item) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      return Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)));
    }
    return item;
  }) + "\n";
}

function receiptKey(planId: string): string {
  return `v1/publications/${planId}.json`;
}

function digest(value: string): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

async function putVaultReceipt(captureId: string, planId: string, markdown: string): Promise<string> {
  const vault = await runtime.getR2Bucket("VAULT_BUCKET");
  const jsonPath = `MedLearn/Data/Captures/${captureId}.json`;
  const markdownPath = `MedLearn/Captures/2026/07/${captureId}.md`;
  const json = `{"capture_id":"${captureId}"}\n`;
  const receipt = canonicalJson({
    receipt_version: "0.1.0",
    publication_plan_id: planId,
    publication_plan_object_digest: `sha256:${"a".repeat(64)}`,
    capture_id: captureId,
    artifacts: [
      { path: jsonPath, media_type: "application/json; charset=utf-8", content_digest: digest(json), byte_length: Buffer.byteLength(json) },
      { path: markdownPath, media_type: "text/markdown; charset=utf-8", content_digest: digest(markdown), byte_length: Buffer.byteLength(markdown) },
    ],
  });
  await vault.put(jsonPath, json, { httpMetadata: { contentType: "application/json; charset=utf-8" } });
  await vault.put(markdownPath, markdown, { httpMetadata: { contentType: "text/markdown; charset=utf-8" } });
  await vault.put(receiptKey(planId), receipt, { httpMetadata: { contentType: "application/json; charset=utf-8" } });
  return markdownPath;
}

afterAll(async () => {
  await runtime.dispose();
  await dispatchMock.close();
});

describe("Cloudflare runtime", () => {
  it("serves public health through workerd", async () => {
    const response = await runtime.dispatchFetch("https://example.test/health");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  it("fails closed in workerd when secrets are absent", async () => {
    const unconfigured = new Miniflare({ modules: true, scriptPath: ".runtime-test/index.js", r2Buckets: ["CONTROL_BUCKET"] });
    try {
      const response = await unconfigured.dispatchFetch("https://example.test/v1/jobs/missing");
      expect(response.status).toBe(503);
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(await response.json()).toEqual({ error: "SERVICE_MISCONFIGURED" });
    } finally {
      await unconfigured.dispose();
    }
  });

  it("uses workerd R2 conditionals for create-only and CAS writes", async () => {
    const bucket = await runtime.getR2Bucket("CONTROL_BUCKET");
    const key = "runtime-r2-conditional";
    const body = readFileSync(resolve("../examples/intake/manual-copd.json"), "utf8");
    const headers = {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "idempotency-key": "runtime-r2-create-only",
    };
    expect((await runtime.dispatchFetch("https://example.test/v1/captures", { method: "POST", headers, body })).status).toBe(202);
    const changed = { ...JSON.parse(body), client_kind: "ios_shortcut" };
    const duplicate = await runtime.dispatchFetch("https://example.test/v1/captures", {
      method: "POST", headers, body: JSON.stringify(changed),
    });
    expect(duplicate.status).toBe(409);

    await bucket.put(key, "one");

    const current = await bucket.head(key);
    expect(current).not.toBeNull();
    expect(await bucket.put(key, "two", { onlyIf: { etagMatches: current!.etag } })).not.toBeNull();
    expect(await bucket.put(key, "three", { onlyIf: { etagMatches: current!.etag } })).toBeNull();
    expect(await (await bucket.get(key))?.text()).toBe("two");
  });

  it("dispatches a capture through workerd R2 instead of reporting storage unavailable", async () => {
    const body = readFileSync(resolve("../examples/intake/manual-copd.json"), "utf8");
    const response = await runtime.dispatchFetch("https://example.test/v1/captures", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "idempotency-key": "runtime-r2-dispatch",
      },
      body,
    });
    expect(response.status).toBe(202);
    expect(await response.json()).toMatchObject({ status: "dispatched" });
  });

  it("rejects a poisoned content-addressed intake before job creation or dispatch", async () => {
    const bucket = await runtime.getR2Bucket("CONTROL_BUCKET");
    const body = readFileSync(resolve("../examples/intake/manual-copd.json"), "utf8");
    const hex = createHash("sha256").update(body).digest("hex");
    const key = `v1/intakes/sha256/${hex}.json`;
    const poisoned = Buffer.from(`${body}\n`, "utf8");
    const before = (await bucket.list({ prefix: "v1/jobs/" })).objects.length;
    await bucket.put(key, poisoned);
    const response = await runtime.dispatchFetch("https://example.test/v1/captures", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "idempotency-key": "runtime-r2-poisoned-intake",
      },
      body,
    });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "INTAKE_STORAGE_CONFLICT" });
    expect(Buffer.from(await (await bucket.get(key))!.arrayBuffer())).toEqual(poisoned);
    expect((await bucket.list({ prefix: "v1/jobs/" })).objects).toHaveLength(before);
  });

  it("reads a canonical vault receipt from Miniflare R2", async () => {
    await putVaultReceipt(`capture_${"c".repeat(32)}`, `publication_plan_${"1".repeat(32)}`, "# runtime manifest\n");
    const response = await runtime.dispatchFetch("https://example.test/v1/vault/manifest", {
      headers: { authorization: `Bearer ${runtimeSyncToken}` },
    });
    expect(response.status).toBe(200);
    const body = await response.json() as { artifacts: unknown[] };
    expect(body.artifacts).toHaveLength(2);
  });

  it("downloads a verified vault artifact from Miniflare R2", async () => {
    const markdown = "# runtime download\n";
    const path = await putVaultReceipt(`capture_${"d".repeat(32)}`, `publication_plan_${"2".repeat(32)}`, markdown);
    const response = await runtime.dispatchFetch(`https://example.test/v1/vault/files?path=${encodeURIComponent(path)}`, {
      headers: { authorization: `Bearer ${runtimeSyncToken}` },
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/markdown; charset=utf-8");
    expect(await response.text()).toBe(markdown);
  });

  it("uses a separate idempotency namespace for v3 converter — no conflict with old v1/v2 records", async () => {
    // Simulate production state: old v1/v2 idempotency records already exist
    // for the same semantic handoff digest, bound to a different intake digest.
    const bucket = await runtime.getR2Bucket("CONTROL_BUCKET");
    const body = readFileSync(resolve("../examples/intake/apl-bootstrap-worker-envelope.json"));
    const intakeDigest = `sha256:${createHash("sha256").update(body).digest("hex")}`;
    const intakeKey = `v1/intakes/sha256/${intakeDigest.slice("sha256:".length)}.json`;

    // The semantic digest that convertHandoff produces for this handoff
    // We construct a v1-style key to simulate old production state
    const handoffSource = JSON.parse(readFileSync(resolve("../examples/intake/apl-bootstrap-sanitized.json"), "utf8"));
    const canonicalHandoff = JSON.stringify(handoffSource, (_k: string, v: unknown) => {
      if (v && typeof v === "object" && !Array.isArray(v)) {
        return Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)));
      }
      return v;
    });
    const handoffDigest = createHash("sha256").update(canonicalHandoff).digest("hex");

    // Old v1 idempotency key: medlearn-handoff-<digest>
    const v1IdempotencyKey = `medlearn-handoff-${handoffDigest}`;
    const v1IdemKey = `v1/idempotency/${createHash("sha256").update(v1IdempotencyKey).digest("hex")}.json`;
    const v2IdempotencyKey = `medlearn-handoff-v2-${handoffDigest}`;
    const v2IdemKey = `v1/idempotency/${createHash("sha256").update(v2IdempotencyKey).digest("hex")}.json`;

    // Preload an old v1 idempotency record pointing to a DIFFERENT (old) intake digest
    const oldIntakeDigest = `sha256:${"f".repeat(64)}`;
    await bucket.put(v1IdemKey, JSON.stringify({
      idempotency_version: "0.1.0",
      job_id: crypto.randomUUID(),
      intake_digest: oldIntakeDigest,
      created_at: new Date(0).toISOString(),
    }));
    await bucket.put(v2IdemKey, JSON.stringify({
      idempotency_version: "0.1.0",
      job_id: crypto.randomUUID(),
      intake_digest: oldIntakeDigest,
      created_at: new Date(0).toISOString(),
    }));

    // Also preload the intake so the submission doesn't fail on intake storage
    await bucket.put(intakeKey, body);

    // Now submit the same intake with the v3 idempotency key.
    // This should succeed because v3 uses a different namespace.
    const v3IdempotencyKey = `medlearn-handoff-v3-${handoffDigest}`;
    const response = await runtime.dispatchFetch("https://example.test/v1/captures", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "idempotency-key": v3IdempotencyKey,
      },
      body,
    });
    expect(response.status).toBe(202); // v3 creates a new job, no conflict
    const job = await response.json() as { job_id: string; status: string };
    expect(job.job_id).toBeTruthy();
    expect(job.status).toBe("dispatched");

    // Verify the old v1 record was NOT touched
    const oldRecord = await bucket.get(v1IdemKey);
    expect(oldRecord).not.toBeNull();
    const oldValue = await oldRecord!.json() as { intake_digest: string };
    expect(oldValue.intake_digest).toBe(oldIntakeDigest);

    const v2Record = await bucket.get(v2IdemKey);
    expect(v2Record).not.toBeNull();
    const v2Value = await v2Record!.json() as { intake_digest: string };
    expect(v2Value.intake_digest).toBe(oldIntakeDigest);

    // Verify the v3 idempotency record was created
    const v3IdemKey = `v1/idempotency/${createHash("sha256").update(v3IdempotencyKey).digest("hex")}.json`;
    const v3Record = await bucket.get(v3IdemKey);
    expect(v3Record).not.toBeNull();
    const v3Value = await v3Record!.json() as { intake_digest: string };
    expect(v3Value.intake_digest).toBe(intakeDigest);

    // Repeated submission with v3 key remains idempotent
    const repeat = await runtime.dispatchFetch("https://example.test/v1/captures", {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "idempotency-key": v3IdempotencyKey,
      },
      body,
    });
    expect(repeat.status).toBe(202);
    const repeatJob = await repeat.json() as { job_id: string };
    expect(repeatJob.job_id).toBe(job.job_id); // Same job — idempotent reuse

    // A different intake under the v2 key with the same key would conflict
    // (but that's the existing idempotency behavior, not a regression)
  });
});
