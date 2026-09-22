/**
 * dsh-sim-plugin — 服务端入口（DSH 0.1.5-rc.2）
 *
 * 职责（少即是多，真相在工程服务 127.0.0.1:8600）：
 *  1. 注册 settings 命名空间 `dsh-sim`（apiUrl / 身份 / 轮询间隔），设置卡片可改。
 *  2. webServer 前缀代理 /dsh-sim/* → 工程 API：面板 iframe 与 DSH 会话同源同端口，
 *     无 CORS、无第二端口防火墙、身份头在此统一注入（开发模式 X-Dev-*）。
 *  3. /dsh-sim/health 健康探针：设置卡片与工作台顶栏可用性徽章的数据源。
 *
 * 明确不做：不复制工程 API 逻辑；不缓存任务状态；不向消息文本泄露凭据。
 */

const NS = "dsh-sim";
const DEFAULT_CONFIG = {
  apiUrl: "http://127.0.0.1:8600/api/v1",
  subject: "dsh-user",
  roles: "EXECUTOR",
  projects: "default",
  pollIntervalMs: 5000,
};

// 与 dsh-settings 的 zod-like schema 对齐（参考 dsh-visual-plugin 0.3.2 用法：
// settings.register(NS, ConfigSchema, { base })。schema 用 dsh-schemastery z）
import z from "@deepseek-ai/schemastery";

const SimConfig = z.object({
  apiUrl: z.string().min(1).default(DEFAULT_CONFIG.apiUrl),
  subject: z.string().min(1).default(DEFAULT_CONFIG.subject),
  roles: z.string().min(1).default(DEFAULT_CONFIG.roles),
  projects: z.string().min(1).default(DEFAULT_CONFIG.projects),
  pollIntervalMs: z.number().min(1000).max(60000).default(DEFAULT_CONFIG.pollIntervalMs),
});

function writeJson(res, code, body) {
  const payload = JSON.stringify(body);
  res.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(payload);
}

function apiRootOf(config) {
  // apiUrl 形如 http://host:8600/api/v1；apiRoot 为 http://host:8600
  const u = new URL(config.apiUrl);
  return u.origin;
}

function upstreamOf(config, req) {
  // 路径映射（定义书 FR-32：只允许两个白名单前缀，防任意路径逃逸）：
  //   /dsh-sim/api/*    → {apiUrl}/*          （工程 API，如 /tasks、/artifacts/{id}/content）
  //   /dsh-sim/panels/* → {apiRoot}/panels/*  （面板静态页，挂在服务根而非 /api/v1 下）
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname !== "/dsh-sim" && !url.pathname.startsWith("/dsh-sim/")) return null;
  const rest = url.pathname.slice("/dsh-sim".length).replace(/^\//, "");
  if (rest.startsWith("api/")) {
    return new URL(rest.slice(4) + url.search, config.apiUrl.replace(/\/+$/, "") + "/");
  }
  if (rest.startsWith("panels/")) {
    return new URL(rest + url.search, apiRootOf(config) + "/");
  }
  return null;
}

async function proxyFetch(config, req, upstream) {
  // 面板自带 X-Dev-* 头时透传（同事各自的身份）；缺省用插件 settings 注入
  const headers = {
    "X-Dev-Subject": req.headers["x-dev-subject"] ?? config.subject,
    "X-Dev-Roles": req.headers["x-dev-roles"] ?? config.roles,
    "X-Dev-Projects": req.headers["x-dev-projects"] ?? config.projects,
  };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    return await fetch(upstream, {
      method: req.method ?? "GET",
      headers,
      signal: controller.signal,
      redirect: "error",
    });
  } finally {
    clearTimeout(timer);
  }
}

function registerSimRoutes(webServer, getConfig) {
  // 健康探针：本地 settings 视图 + 上游连通性
  webServer.register({
    kind: "prefix",
    path: "/dsh-sim/health",
    async handler(req, res) {
      const config = getConfig();
      let upstream = null;
      let ok = false;
      let detail = "";
      try {
        const probe = await proxyFetch(config, req, new URL("capabilities?limit=1", config.apiUrl.replace(/\/+$/, "") + "/"));
        ok = probe.ok;
        detail = `upstream http ${probe.status}`;
      } catch (err) {
        detail = `upstream unreachable: ${err?.message ?? err}`;
      }
      writeJson(res, 200, {
        plugin: NS,
        version: "0.1.0",
        apiUrl: config.apiUrl,
        upstream_ok: ok,
        detail,
      });
    },
  });

  // 面板静态页与 API 的通用前缀代理（GET only，POST 走 DSH 会话内工具或受信面板）
  // 注意：kind:"prefix" 的 path 必须不带尾斜杠（"/dsh-sim"），
  // 带尾斜杠的 "/dsh-sim/" 只匹配字面根路径，子路径 404（参照
  // dsh-visual-plugin 的 "/vision-bridge/videos" 无前导/尾随斜杠写法）。
  webServer.register({
    kind: "prefix",
    path: "/dsh-sim",
    async handler(req, res) {
      const config = getConfig();
      const upstream = upstreamOf(config, req);
      if (upstream === null) {
        writeJson(res, 403, { code: "FORBIDDEN", message: "仅代理 /dsh-sim/ 前缀下的工程资源" });
        return;
      }
      try {
        const r = await proxyFetch(config, req, upstream);
        const body = Buffer.from(await r.arrayBuffer());
        res.writeHead(r.status, {
          "content-type": r.headers.get("content-type") ?? "application/octet-stream",
          "cache-control": "no-store",
        });
        res.end(body);
      } catch (err) {
        writeJson(res, 503, {
          code: "UNAVAILABLE",
          message: "工程服务不可用（检查设置中的 apiUrl 与服务进程）",
          retryable: true,
          detail: String(err?.message ?? err),
        });
      }
    },
  });
}

function apply(ctx) {
  const webServer = ctx.get("webServer");
  const settings = ctx.get("settings");
  const scope = settings.register(NS, SimConfig, { base: DEFAULT_CONFIG });
  registerSimRoutes(webServer, () => scope.get());
}

export { apply };
