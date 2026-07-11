import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Miniflare } from "miniflare";
import { MockAgent } from "undici";

let runtime: Miniflare;
const token = "runtime-ingest-secret-that-is-at-least-32-bytes";
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
    r2Buckets: ["CONTROL_BUCKET"],
    bindings: {
      MEDLEARN_INGEST_TOKEN: token,
      GITHUB_ACTIONS_DISPATCH_TOKEN: "runtime-github-token",
    },
    fetchMock: dispatchMock,
  });
});

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
});
