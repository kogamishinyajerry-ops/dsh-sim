/**
 * dsh-sim panels · shared/components.js
 * 共享 UI 组件：状态徽章（五维状态）、证据链节点、blocker 列表项、
 * digest 短码、trace_id 行。
 *
 * 状态色纪律（CONVENTIONS §0 / 定义书 §执行工作台交互定义）：
 *  - 每个徽章 = 英文枚举原文 + 中文注释 + 颜色；绝不用一个绿勾代替多维判断；
 *  - 颜色不是唯一状态表达，徽章始终带文字；
 *  - MOCK → 琥珀色徽章；未知/失联 → 条纹底 + "状态未知"；NOT_RUN → 灰色虚线徽章。
 */

/* ---------- 基础工具 ---------- */

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

/* ---------- 状态徽章字典（英文枚举原文 + 中文注释） ---------- */

export const STATE_BADGES = {
  // TaskFlow（任务流程）
  DRAFT:              { zh: '草稿',     cls: 'b-muted' },
  PREPARING:          { zh: '准备中',   cls: 'b-info' },
  READY:              { zh: '待确认',   cls: 'b-info' },
  AUTHORIZED:         { zh: '已授权',   cls: 'b-info' },
  ACTIVE:             { zh: '执行中',   cls: 'b-active' },
  READY_FOR_REVIEW:   { zh: '待审查',   cls: 'b-warn' },
  IN_REVIEW:          { zh: '审查中',   cls: 'b-warn' },
  ACCEPTED:           { zh: '已接受',   cls: 'b-ok' },
  CHANGES_REQUESTED:  { zh: '已退回',   cls: 'b-warn' },
  REJECTED:           { zh: '已拒绝',   cls: 'b-err' },
  // Execution（执行）
  QUEUED:             { zh: '排队中',     cls: 'b-muted' },
  WAITING_RESOURCE:   { zh: '等待资源',   cls: 'b-warn' },
  LEASED:             { zh: '已领取',     cls: 'b-info' },
  STARTING:           { zh: '启动中',     cls: 'b-info' },
  RUNNING:            { zh: '运行中',     cls: 'b-active' },
  CANCELLING:         { zh: '取消中',     cls: 'b-warn' },
  COLLECTING:         { zh: '收集产物',   cls: 'b-info' },
  SUCCEEDED:          { zh: '执行完成(≠数值通过)', cls: 'b-info' },
  FAILED:             { zh: '执行失败',   cls: 'b-err' },
  CANCELLED:          { zh: '已取消',     cls: 'b-muted' },
  LOST:               { zh: '失联(状态未知)', cls: 'b-lost' },
  // Numerical（数值）
  NOT_CHECKED:        { zh: '未检查',   cls: 'b-notrun' },
  PASS:               { zh: '数值通过', cls: 'b-ok' },
  FAIL:               { zh: '数值失败', cls: 'b-err' },
  INSUFFICIENT:       { zh: '证据不足', cls: 'b-warn' },
  // Applicability（适用性）
  IN_SCOPE:           { zh: '范围内',   cls: 'b-ok' },
  OUT_OF_SCOPE:       { zh: '范围外',   cls: 'b-err' },
  UNCONFIRMED:        { zh: '范围未确认', cls: 'b-warn' },
  // Review（审查）
  NOT_SUBMITTED:      { zh: '未提交',   cls: 'b-notrun' },
  PENDING:            { zh: '待审',     cls: 'b-warn' },
  // Validity（有效性）
  CURRENT:            { zh: '当前有效', cls: 'b-ok' },
  STALE:              { zh: '已过期',   cls: 'b-err' },
  // evidence_mode
  REAL:               { zh: '真实',     cls: 'b-ok' },
  MOCK:               { zh: '模拟数据', cls: 'b-mock' },
  UNKNOWN:            { zh: '状态未知', cls: 'b-unknown' },
  // 通用
  NOT_RUN:            { zh: '未执行',   cls: 'b-notrun' },
};

/**
 * 状态徽章：<span class="badge b-xxx">ENUM · 中文</span>
 * 未识别状态 → UNKNOWN 条纹底 + "状态未知"，不静默猜色。
 */
export function badge(state, { zhOverride = null, title = null } = {}) {
  const key = state == null ? 'UNKNOWN' : String(state);
  const meta = STATE_BADGES[key] || STATE_BADGES.UNKNOWN;
  const label = `${key} · ${zhOverride || meta.zh}`;
  const span = el('span', { class: `badge ${meta.cls}`, text: label });
  span.title = title || `状态枚举: ${key}`;
  if (key === 'MOCK') span.classList.add('b-mock-italic');
  return span;
}

/** 徽章 HTML 字符串版本（用于 innerHTML 场景） */
export function badgeHtml(state, opts = {}) {
  const key = state == null ? 'UNKNOWN' : String(state);
  const meta = STATE_BADGES[key] || STATE_BADGES.UNKNOWN;
  const italic = key === 'MOCK' ? ' b-mock-italic' : '';
  return `<span class="badge ${meta.cls}${italic}" title="状态枚举: ${esc(key)}">${esc(key)} · ${esc(opts.zhOverride || meta.zh)}</span>`;
}

/** evidence_mode 顶栏徽章（REAL/MOCK/UNKNOWN） */
export function evidenceModeBadge(mode) {
  const key = mode || 'UNKNOWN';
  const node = badge(STATE_BADGES[key] ? key : 'UNKNOWN');
  node.classList.add('badge-evidence');
  return node;
}

/* ---------- digest 短码 ---------- */

/**
 * digest 短码：前 12 位 + title 属性全量；可复制。
 * 形如 sha256:abcdef... → 显示 sha256:abcdef123456（截断至 12 位 hex 部分）。
 */
export function digestShort(digest) {
  if (!digest) return el('span', { class: 'digest digest-empty', text: '（无摘要）' });
  const s = String(digest);
  const prefix = s.startsWith('sha256:') ? 'sha256:' : '';
  const hex = prefix ? s.slice(7) : s;
  const short = hex.length > 12 ? hex.slice(0, 12) + '…' : hex;
  const node = el('code', { class: 'digest', text: prefix + short });
  node.title = s; // title 属性全量
  node.addEventListener('click', () => {
    navigator.clipboard?.writeText(s).catch(() => {});
  });
  return node;
}

/* ---------- trace_id 行 ---------- */

export function traceRow(traceId) {
  const row = el('div', { class: 'trace-row' }, [
    el('span', { class: 'trace-label', text: 'trace_id：' }),
    el('code', { class: 'trace-id', text: traceId || '（无）' }),
  ]);
  if (traceId) {
    const btn = el('button', { class: 'btn btn-xs', type: 'button', text: '复制' });
    btn.addEventListener('click', () => navigator.clipboard?.writeText(traceId).catch(() => {}));
    row.append(btn);
  }
  return row;
}

/* ---------- blocker 列表项 ---------- */

/**
 * blocker 列表项：阻塞项 + 责任人 + 可采取动作（定义书 §阻塞不是静默跳过）。
 * blocker 形状（服务端 blockers[] 元素，宽松解析）：
 *   { code|kind, message|description, responsible|owner, action|hint }
 */
export function blockerItem(b) {
  const code = b.code || b.kind || b.type || 'BLOCKER';
  const msg = b.message || b.description || b.detail || '';
  const responsible = b.responsible || b.owner || b.assignee || '（未指派）';
  const action = b.action || b.hint || b.next_step || '请联系责任人处理';
  return el('li', { class: 'blocker-item' }, [
    el('div', { class: 'blocker-head' }, [
      el('span', { class: 'badge b-err', text: String(code) }),
      el('span', { class: 'blocker-msg', text: msg }),
    ]),
    el('div', { class: 'blocker-meta' }, [
      el('span', {}, [document.createTextNode('责任人：'), el('strong', { text: responsible })]),
      el('span', {}, [document.createTextNode('可采取动作：'), el('span', { text: action })]),
    ]),
  ]);
}

export function blockerList(blockers) {
  if (!blockers || blockers.length === 0) {
    return el('p', { class: 'ok-line', text: '当前无阻塞项。' });
  }
  return el('ul', { class: 'blocker-list' }, blockers.map(blockerItem));
}

/* ---------- 证据链节点 ---------- */

/**
 * 证据链节点：结论→指标定义→原始数值→实际设置→方法范围 的逐级节点。
 * item: { level, title, summary, digest?, artifact_id?, expandable? }
 */
export function evidenceChainNode(item) {
  const head = el('div', { class: 'ec-node-head' }, [
    el('span', { class: 'ec-level', text: item.level || '' }),
    el('strong', { text: item.title || '' }),
  ]);
  const body = el('div', { class: 'ec-node-body' }, [
    el('p', { class: 'ec-summary', text: item.summary || '' }),
  ]);
  if (item.digest) body.append(el('div', { class: 'ec-digest' }, [document.createTextNode('摘要：'), digestShort(item.digest)]));
  if (item.artifact_id) {
    const link = el('a', { class: 'ec-artifact', href: '#', text: `artifact: ${item.artifact_id}` });
    if (item.artifact_url) link.href = item.artifact_url;
    body.append(link);
  }
  const node = el('div', { class: 'ec-node' }, [head, body]);
  if (item.expandable && Array.isArray(item.children) && item.children.length) {
    const toggle = el('button', { class: 'btn btn-xs ec-toggle', type: 'button', text: '展开下级 ▾' });
    const childWrap = el('div', { class: 'ec-children', hidden: '' }, item.children.map(evidenceChainNode));
    toggle.addEventListener('click', () => {
      const hidden = childWrap.hasAttribute('hidden');
      if (hidden) { childWrap.removeAttribute('hidden'); toggle.textContent = '收起下级 ▴'; }
      else { childWrap.setAttribute('hidden', ''); toggle.textContent = '展开下级 ▾'; }
    });
    head.append(toggle);
    node.append(childWrap);
  }
  return node;
}

export function evidenceChain(items) {
  if (!items || items.length === 0) {
    return el('p', { class: 'muted-line', text: '暂无证据链数据（API 未返回或服务离线）。' });
  }
  return el('div', { class: 'evidence-chain' }, items.map(evidenceChainNode));
}

/* ---------- 离线/降级横幅 ---------- */

export function offlineBanner(message) {
  return el('div', { class: 'offline-banner', role: 'alert' }, [
    el('strong', { text: '工程服务不可用' }),
    el('span', { text: ` — ${message || '无法连接工程服务，以下为已缓存/空状态，不代表真实状态。'}` }),
  ]);
}

/** 错误对象渲染（含 trace_id，定义书：有问题时提供 trace_id、证据位置和人工下一步） */
export function errorBlock(err) {
  const wrap = el('div', { class: 'error-block', role: 'alert' }, [
    el('div', {}, [
      el('span', { class: 'badge b-err', text: err.code || 'ERROR' }),
      el('span', { text: ` ${err.message || '未知错误'}` }),
    ]),
    el('div', { class: 'error-hint', text: err.retryable ? '该错误可重试（有界重试）。' : '该错误不可自动重试，请人工处理。' }),
    traceRow(err.trace_id),
  ]);
  return wrap;
}

/* ---------- 心跳/时间 ---------- */

export function fmtTime(iso) {
  if (!iso) return '（无）';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString('zh-CN', { hour12: false });
}

/** 距当前秒数；无法解析返回 null */
export function secondsSince(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}
