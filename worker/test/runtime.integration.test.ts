import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Miniflare } from "miniflare";

let runtime: Miniflare;

beforeAll(() => {
  runtime = new Miniflare({
    modules: true,
    scriptPath: ".runtime-test/index.js",
    r2Buckets: ["CONTROL_BUCKET"],
  });
});

afterAll(async () => runtime.dispose());

describe("Cloudflare runtime", () => {
  it("serves public health through workerd", async () => {
    const response = await runtime.dispatchFetch("https://example.test/health");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  it("fails closed in workerd when secrets are absent", async () => {
    const response = await runtime.dispatchFetch("https://example.test/v1/jobs/missing");
    expect(response.status).toBe(503);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ error: "SERVICE_MISCONFIGURED" });
  });
});
