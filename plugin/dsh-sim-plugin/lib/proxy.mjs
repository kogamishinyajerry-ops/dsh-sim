/** Panel transport only. X-Dev identity remains LOCAL DEVELOPMENT ONLY. */
export class ProxyRequestError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function roots(config) {
  const api = new URL(config.apiUrl.replace(/\/+$/, "") + "/");
  if (!["http:", "https:"].includes(api.protocol) || api.username || api.password || api.search || api.hash) {
    throw new ProxyRequestError(503, "UNAVAILABLE", "Invalid engineering API configuration");
  }
  return { api, panels: new URL("/panels/", api) };
}

function within(target, root) {
  return target.origin === root.origin && target.pathname.startsWith(root.pathname)
    && !target.username && !target.password;
}

export function upstreamOf(config, req) {
  try {
    const { api, panels } = roots(config);
    const url = new URL(req.url ?? "/", "http://localhost");
    let root, relative;
    if (url.pathname.startsWith("/dsh-sim/api/")) {
      root = api;
      relative = url.pathname.slice("/dsh-sim/api/".length);
    } else if (url.pathname.startsWith("/dsh-sim/panels/")) {
      root = panels;
      relative = url.pathname.slice("/dsh-sim/panels/".length);
    } else return null;
    if (/^[\\/]/.test(relative)) return null;
    const target = new URL(relative + url.search, root);
    return within(target, root) ? target : null;
  } catch {
    return null;
  }
}

async function readBody(req, limit) {
  const declared = Number(req.headers?.["content-length"] ?? 0);
  if (!Number.isFinite(declared) || declared < 0) {
    throw new ProxyRequestError(400, "VALIDATION", "Invalid Content-Length");
  }
  if (declared > limit) throw new ProxyRequestError(413, "PAYLOAD_TOO_LARGE", "Panel request body exceeds limit");
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > limit) throw new ProxyRequestError(413, "PAYLOAD_TOO_LARGE", "Panel request body exceeds limit");
    chunks.push(bytes);
  }
  return Buffer.concat(chunks);
}

export async function proxyFetch(config, req, upstream, {
  fetchImpl = globalThis.fetch, maxBodyBytes = 1024 * 1024, timeoutMs = 15000,
} = {}) {
  const { api, panels } = roots(config);
  const target = new URL(upstream);
  const isApi = within(target, api);
  if (!isApi && !within(target, panels)) {
    throw new ProxyRequestError(403, "FORBIDDEN", "Upstream outside configured engineering resources");
  }
  const method = (req.method ?? "GET").toUpperCase();
  if (!(isApi ? ["GET", "HEAD", "POST"] : ["GET", "HEAD"]).includes(method)) {
    throw new ProxyRequestError(405, "METHOD_NOT_ALLOWED", "Unsupported panel proxy method");
  }
  const incoming = req.headers ?? {};
  // Preserve the existing dev-only identity semantics. Never claim this is IdP auth.
  const headers = {
    "X-Dev-Subject": incoming["x-dev-subject"] ?? config.subject,
    "X-Dev-Roles": incoming["x-dev-roles"] ?? config.roles,
    "X-Dev-Projects": incoming["x-dev-projects"] ?? config.projects,
  };
  for (const name of ["accept", "content-type", "idempotency-key", "x-dev-project"]) {
    if (typeof incoming[name] === "string") headers[name] = incoming[name];
  }
  // Bounded buffering for small JSON panel commands, not a large-artifact uploader.
  const body = method === "POST" ? await readBody(req, maxBodyBytes) : undefined;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetchImpl(target, { method, headers, body, signal: controller.signal, redirect: "error" });
  } finally {
    clearTimeout(timer);
  }
}
