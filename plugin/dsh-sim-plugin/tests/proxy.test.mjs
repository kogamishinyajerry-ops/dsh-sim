import assert from "node:assert/strict";
import { Readable } from "node:stream";
import test from "node:test";
import { upstreamOf, proxyFetch } from "../lib/proxy.mjs";

const config = { apiUrl: "http://127.0.0.1:8600/api/v1", subject: "dev-user", roles: "EXECUTOR", projects: "default" };
function request(url, method = "GET", body = "", headers = {}) {
  return Object.assign(Readable.from(body ? [Buffer.from(body)] : []), { url, method, headers });
}
function recorder() {
  const calls = [];
  return {
    calls,
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      return new Response('{"ok":true}', { status: 201 });
    },
  };
}

test("panel JSON bytes and idempotency key survive the proxy", async () => {
  const body = JSON.stringify({ description: "缓冲腔复核", revision: 1 });
  const req = request("/dsh-sim/api/confirmations", "POST", body, {
    "content-type": "application/json", "idempotency-key": "panel-request-123",
    "x-dev-subject": "reviewer", "x-dev-roles": "REVIEWER", "x-dev-project": "proj_a",
  });
  const record = recorder();
  const response = await proxyFetch(config, req, upstreamOf(config, req), record);
  assert.equal(response.status, 201);
  const { options } = record.calls[0];
  assert.equal(options.body.toString("utf8"), body);
  assert.equal(options.headers["idempotency-key"], "panel-request-123");
  assert.equal(options.headers["content-type"], "application/json");
  assert.equal(options.headers["X-Dev-Subject"], "reviewer");
  assert.equal(options.headers["x-dev-project"], "proj_a");
});

test("GET and query parameters remain read-only", async () => {
  const req = request("/dsh-sim/api/tasks?limit=10");
  const record = recorder();
  await proxyFetch(config, req, upstreamOf(config, req), record);
  assert.equal(record.calls[0].url, "http://127.0.0.1:8600/api/v1/tasks?limit=10");
  assert.equal(record.calls[0].options.body, undefined);
  assert.equal(record.calls[0].options.headers["X-Dev-Subject"], config.subject);
  assert.equal(record.calls[0].options.redirect, "error");
});

test("static panel paths resolve under /panels/", () => {
  assert.equal(String(upstreamOf(config, request("/dsh-sim/panels/reviewer/index.html?api=x"))),
    "http://127.0.0.1:8600/panels/reviewer/index.html?api=x");
});

for (const path of ["/elsewhere", "/dsh-sim/other/file", "/dsh-sim/api///example.invalid/path", "/dsh-sim/api//outside", "/dsh-sim/api/%2e%2e/private"]) {
  test(`reject nonallowlisted or escaping path: ${path}`, () => {
    assert.equal(upstreamOf(config, request(path)), null);
  });
}

test("direct callers cannot supply an external upstream", async () => {
  const record = recorder();
  await assert.rejects(proxyFetch(config, request("/dsh-sim/api/tasks"), "https://example.invalid/api/v1/tasks", record), { status: 403 });
  assert.equal(record.calls.length, 0);
});

for (const [path, method] of [["/dsh-sim/panels/file", "POST"], ["/dsh-sim/api/tasks", "TRACE"]]) {
  test(`reject unsupported method ${method} on ${path}`, async () => {
    const req = request(path, method, "body");
    const record = recorder();
    await assert.rejects(proxyFetch(config, req, upstreamOf(config, req), record), { status: 405 });
    assert.equal(record.calls.length, 0);
  });
}

for (const declared of [undefined, "999"]) {
  test(`reject oversized body before fetch (declared=${declared})`, async () => {
    const req = request("/dsh-sim/api/tasks", "POST", "too large", declared ? { "content-length": declared } : {});
    const record = recorder();
    await assert.rejects(proxyFetch(config, req, upstreamOf(config, req), { ...record, maxBodyBytes: 4 }), { status: 413 });
    assert.equal(record.calls.length, 0);
  });
}

test("do not blindly relay hop-by-hop headers or credentials", async () => {
  const req = request("/dsh-sim/api/tasks", "GET", "", {
    authorization: "must-not-relay", cookie: "must-not-relay", host: "other.invalid", connection: "keep-alive",
  });
  const record = recorder();
  await proxyFetch(config, req, upstreamOf(config, req), record);
  for (const name of ["authorization", "cookie", "host", "connection"]) {
    assert.equal(record.calls[0].options.headers[name], undefined);
  }
});

test("upstream failures propagate rather than appearing successful", async () => {
  const req = request("/dsh-sim/api/tasks");
  await assert.rejects(proxyFetch(config, req, upstreamOf(config, req), {
    fetchImpl: async () => { throw new Error("upstream offline"); },
  }), /upstream offline/);
});

test("real loopback HTTP upstream receives POST bytes and idempotency key", async () => {
  const { createServer } = await import("node:http");
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    res.writeHead(201, { "content-type": "application/json" });
    res.end(JSON.stringify({ method: req.method, path: req.url, body: Buffer.concat(chunks).toString(), key: req.headers["idempotency-key"] }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const local = { ...config, apiUrl: `http://127.0.0.1:${server.address().port}/api/v1` };
    const body = JSON.stringify({ revision: 1, purpose: "测试" });
    const req = request("/dsh-sim/api/confirmations", "POST", body, { "content-type": "application/json", "idempotency-key": "loopback-key-123" });
    const response = await proxyFetch(local, req, upstreamOf(local, req));
    assert.equal(response.status, 201);
    assert.deepEqual(await response.json(), { method: "POST", path: "/api/v1/confirmations", body, key: "loopback-key-123" });
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
