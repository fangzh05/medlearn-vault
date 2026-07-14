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
});
