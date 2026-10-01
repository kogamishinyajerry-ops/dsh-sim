/**
 * dsh-sim panels · shared/api.js
 * 工程 API fetch 封装（contracts/openapi.v0.1.yaml 的客户端投影）。
 *
 * 降级纪律（CONVENTIONS §0 / 定义书 §锁版接入方式）：
 *  - 网络失败/服务不可达 → 返回 { offline: true, ... }，绝不抛异常打死页面；
 *    调用方负责显示"工程服务不可用"横幅，不允许静默空白。
 *  - HTTP 错误 → 返回 { error: { code, message, retryable, trace_id, details }, status }。
 *  - 成功 → 返回 { data }。
 *
 * 身份头：X-Dev-Subject / X-Dev-Roles 仅为开发模式注入。
 * 生产环境必须走受信会话（可信 UI 会话 + 短期代理凭据），本头不会被服务端采信。
 */
// 开发模式，生产走受信会话
export const DEV_HEADERS_NOTE = '开发模式，生产走受信会话';

const DEFAULT_BASE = 'http://127.0.0.1:8600/api/v1';

/** base URL 可配：?api= 查询参数 > localStorage dshsim.api_base > 默认 */
export function getApiBase() {
  try {
    const q = new URLSearchParams(window.location.search).get('api');
    if (q) return q.replace(/\/$/, '');
    const ls = window.localStorage.getItem('dshsim.api_base');
    if (ls) return ls.replace(/\/$/, '');
  } catch (_) { /* 无 window 环境时回落默认 */ }
  return DEFAULT_BASE;
}

export function setApiBase(url) {
  window.localStorage.setItem('dshsim.api_base', url.replace(/\/$/, ''));
}

/**
 * dev 身份头。取值链：调用方传入 > localStorage（dsh-sim 插件设置卡片写入）> 默认。
 * 生产走受信会话，此处仅开发联调用。
 */
export function devIdentityHeaders({ subject, roles, projects } = {}) {
  const ls = (k, d) => {
    try { return window.localStorage.getItem(k) || d; } catch { return d; }
  };
  return {
    'X-Dev-Subject': subject ?? ls('dshsim.subject', 'dev-user'),
    'X-Dev-Roles': roles ?? ls('dshsim.roles', 'EXECUTOR'),
    // 默认 proj_a = 专用测试项目（与 MCP DSH_SIM_AGENT_PROJECTS 默认一致）；
    // 显式配置（调用方传入 > localStorage dshsim.projects）优先于默认值。
    'X-Dev-Projects': projects ?? ls('dshsim.projects', 'proj_a'),
  };
}

/** 生成幂等键（主体+项目+action+key 唯一约束由服务端落实） */
export function newIdempotencyKey(prefix = 'panel') {
  const rand = (crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${rand}`;
}

/**
 * 统一请求。
 * @returns {Promise<{data?:any, error?:object, status?:number, offline?:boolean}>}
 */
export async function apiFetch(path, {
  method = 'GET',
  body = undefined,
  identity = {},
  idempotencyKey = undefined,
  base = getApiBase(),
  signal = undefined,
  timeoutMs = 15000,
} = {}) {
  const headers = {
    'Accept': 'application/json',
    ...devIdentityHeaders(identity),
  };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const linkedSignal = signal || ctrl.signal;

  let resp;
  try {
    resp = await fetch(`${base}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: linkedSignal,
    });
  } catch (e) {
    clearTimeout(timer);
    // 网络层失败（DNS/拒绝连接/超时中止/CORS）：返回 offline，不抛死
    return { offline: true, message: `无法连接工程服务（${base}）：${e.name === 'AbortError' ? '请求超时' : e.message}` };
  }
  clearTimeout(timer);

  let payload = null;
  const text = await resp.text().catch(() => '');
  if (text) {
    try { payload = JSON.parse(text); } catch (_) { payload = null; }
  }

  if (resp.ok) {
    return { data: payload, status: resp.status };
  }

  // 统一错误对象 {code,message,retryable,trace_id,details}
  const err = (payload && typeof payload === 'object' && payload.code)
    ? payload
    : { code: `HTTP_${resp.status}`, message: (payload && payload.message) || resp.statusText || '请求失败', retryable: resp.status >= 500, trace_id: null, details: {} };
  return { error: err, status: resp.status };
}

/** 便捷 GET */
export function apiGet(path, opts = {}) {
  return apiFetch(path, { ...opts, method: 'GET' });
}

/** 便捷 POST（自动带幂等键；产生副作用的 POST 必带 Idempotency-Key，见 CONVENTIONS §3.4） */
export function apiPost(path, body, opts = {}) {
  return apiFetch(path, {
    ...opts,
    method: 'POST',
    body,
    idempotencyKey: opts.idempotencyKey || newIdempotencyKey(opts.actionPrefix || 'panel'),
  });
}
