/**
 * dsh-sim · panels/monitor/app.js（仿真监控台）
 * 只读实况面板：聚合所有任务的运行中 Run，展示心跳、事件流与残差迷你图。
 *
 * 数据依赖（GET /tasks → /tasks/{id}/runs，runs 投影含 events 数组）：
 *   每个 run: { run_id, task_id?, variant_id, condition_id, execution_state,
 *               latest_event_at?, events: [{event_seq,kind,payload,occurred_at}] }
 *   HEARTBEAT payload 可能带 stage / log_path|log_location / residuals|history。
 *   payload 缺字段一律优雅降级为"无数据/未上报"文案，绝不画假数据、不报错死页面。
 *
 * 降级纪律（CONVENTIONS §0）：
 *   - 服务离线 → offline 横幅 + 空态卡片；evidence_mode 未知 → UNKNOWN 条纹徽章。
 *   - 残差流缺失 → "暂无残差流（该作业类型未上报）"，不画假 sparkline。
 *   - MOCK 数据 → 琥珀徽章常驻顶栏。
 */
import { apiGet } from '../shared/api.js';
import {
  el, badge, evidenceModeBadge, offlineBanner, errorBlock, fmtTime, secondsSince,
} from '../shared/components.js';

/* ---------- 常量 ---------- */
const POLL_MS = 3000;                      // 监控页比执行台更快（3s）
const HEARTBEAT_LOST_AFTER_S = 45;         // 与 executor 一致：15s×3 失联阈值
const RECENT_EVENTS_N = 8;                 // 每卡事件时间线条数
const SPARK_POINTS = 120;                  // 残差迷你图取最近 N 点
const ACTIVE_EXEC_STATES = new Set([
  'QUEUED', 'WAITING_RESOURCE', 'LEASED', 'STARTING',
  'RUNNING', 'CANCELLING', 'COLLECTING',
]);
const EVENT_KINDS = new Set([              // 事件 kind 合法集（超出按 UNKNOWN 徽章渲染）
  'STARTING', 'RUNNING', 'HEARTBEAT', 'COMPLETED', 'FAILED', 'CANCELLED',
]);

const state = {
  tasks: null,          // /tasks 响应 items（null=未加载，{__error}=失败）
  runIndex: new Map(),  // run_id -> { run, events, error } 聚合缓存
  taskErrors: new Map(),// task_id -> error（runs 读取失败，逐卡降级）
  offline: false,
  lastPollAt: null,
  loading: false,
  firstLoadDone: false,
};

const $ = (id) => document.getElementById(id);

/* ---------- 数据获取 ---------- */
async function tryGet(path, areaLabel) {
  const r = await apiGet(path, { identity: { subject: 'monitor-viewer', roles: 'VIEWER' } });
  if (r.offline) { state.offline = true; return null; }
  if (r.error) {
    console.warn(`[monitor] ${areaLabel} 读取失败`, r.error);
    return { __error: r.error };
  }
  return r.data;
}

/**
 * 拉取全部任务 → 并发拉取每个任务的 runs（带 events 投影）。
 * 逐任务降级：单个 /runs 失败只影响该任务区域，不拖死整页。
 */
async function pollAll() {
  if (state.loading) return;
  state.loading = true;
  state.offline = false;
  const tasksData = await tryGet('/tasks', '任务列表');
  if (state.offline) { state.loading = false; renderAll(); return; }

  state.tasks = tasksData;
  state.taskErrors.clear();
  const nextIndex = new Map();
  const taskItems = (tasksData && !tasksData.__error && Array.isArray(tasksData.items)) ? tasksData.items : [];

  await Promise.all(taskItems.map(async (t) => {
    const taskId = t.task_id;
    const data = await tryGet(`/tasks/${encodeURIComponent(taskId)}/runs`, `任务 ${taskId} runs`);
    if (state.offline) return;
    if (!data || data.__error) {
      // 保留上一轮该任务已知的 run（缓存态），并记录错误
      state.taskErrors.set(taskId, data?.__error || { code: 'NO_RESPONSE', message: '无响应' });
      for (const [rid, ent] of state.runIndex) {
        if (ent.taskId === taskId) nextIndex.set(rid, ent);
      }
      return;
    }
    for (const run of Array.isArray(data.items) ? data.items : []) {
      const events = Array.isArray(run.events)
        ? [...run.events].sort((a, b) => (a.event_seq || 0) - (b.event_seq || 0))
        : [];
      nextIndex.set(run.run_id, { run, events, taskId });
    }
  }));

  if (state.offline) { state.loading = false; renderAll(); return; }
  state.runIndex = nextIndex;
  state.lastPollAt = new Date();
  state.firstLoadDone = true;
  state.loading = false;
  renderAll();
}

/* ---------- 派生数据 ---------- */
function latestEventAt(run, events) {
  const fromRun = run.latest_event_at || run.last_event_at || null;
  const fromEvents = events.length ? events[events.length - 1].occurred_at : null;
  return fromRun || fromEvents || null;
}

/** 活跃 run（未终态）在前，按心跳新旧排序 */
function activeRuns() {
  const out = [];
  for (const entry of state.runIndex.values()) {
    if (ACTIVE_EXEC_STATES.has(entry.run.execution_state)) out.push(entry);
  }
  return out.sort((a, b) => {
    const ta = new Date(latestEventAt(a.run, a.events) || 0).getTime() || 0;
    const tb = new Date(latestEventAt(b.run, b.events) || 0).getTime() || 0;
    return tb - ta;
  });
}

/** 最近完成的 run（SUCCEEDED/FAILED/CANCELLED），按最后事件时间倒序 */
function recentFinishedRuns(limit = 5) {
  const out = [];
  for (const entry of state.runIndex.values()) {
    if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(entry.run.execution_state)) out.push(entry);
  }
  return out
    .sort((a, b) => {
      const ta = new Date(latestEventAt(a.run, a.events) || 0).getTime() || 0;
      const tb = new Date(latestEventAt(b.run, b.events) || 0).getTime() || 0;
      return tb - ta;
    })
    .slice(0, limit);
}

/** 从 HEARTBEAT payload 宽松提取残差序列（residuals|history|residual_history） */
function extractResiduals(events) {
  const seq = [];
  for (const e of events) {
    if (e.kind !== 'HEARTBEAT') continue;
    const p = e.payload || {};
    const arr = p.residuals || p.history || p.residual_history || null;
    if (Array.isArray(arr)) {
      for (const v of arr) {
        const n = typeof v === 'number' ? v : Number(v?.value ?? v?.residual ?? NaN);
        if (Number.isFinite(n)) seq.push(n);
      }
    } else if (typeof p.residual === 'number' && Number.isFinite(p.residual)) {
      seq.push(p.residual); // 单值心跳形态
    }
  }
  return seq;
}

function lastStage(run, events) {
  for (let i = events.length - 1; i >= 0; i--) {
    const st = events[i].payload?.stage;
    if (st) return st;
  }
  return null;
}

function lastHeartbeat(events) {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].kind === 'HEARTBEAT') return events[i];
  }
  return null;
}

/* ---------- 顶栏 ---------- */
function renderTopbar() {
  const slot = $('evidence-mode-slot');
  slot.replaceChildren();
  const modes = new Set();
  for (const entry of state.runIndex.values()) {
    if (entry.run.evidence_mode) modes.add(entry.run.evidence_mode);
  }
  let mode = 'UNKNOWN';
  if (modes.size === 1) mode = [...modes][0];
  else if (modes.size > 1) mode = 'MOCK'; // 混合来源按 MOCK 保守呈现
  if (state.offline) mode = 'UNKNOWN';
  slot.append(evidenceModeBadge(mode));
  if (mode === 'MOCK') slot.append(el('span', { class: 'mock-watermark', text: 'MOCK' }));
}

function renderServiceStatus() {
  const slot = $('service-status-slot');
  slot.replaceChildren();
  const cls = state.offline ? 'b-err' : (state.firstLoadDone ? 'b-ok' : 'b-unknown');
  const label = state.offline
    ? 'OFFLINE · 服务离线'
    : (state.firstLoadDone ? 'ONLINE · 服务在线' : 'UNKNOWN · 待探测');
  slot.append(el('span', { class: `badge ${cls}`, text: label, title: '工程服务连通状态' }));
}

function renderOffline() {
  const slot = $('offline-slot');
  slot.replaceChildren();
  if (state.offline) {
    slot.append(offlineBanner('无法连接工程服务（127.0.0.1:8600）。监控台显示为空态/缓存态，不代表真实作业状态；每 3s 自动重试。'));
  }
}

function renderPollStatus() {
  const p = $('poll-status');
  p.textContent = state.lastPollAt
    ? `上次刷新 ${fmtTime(state.lastPollAt.toISOString())} · 轮询间隔 3s${document.hidden ? '（页面不可见，已暂停）' : ''}`
    : '等待首次刷新…';
}

/* ---------- 残差迷你图（手写 SVG，无外部库） ---------- */
/**
 * log10 刻度 sparkline。数据取最近 SPARK_POINTS 点；|x| 下限 1e-30 防 log(0)。
 * y = log10(max(|x|,1e-30))；空/无效数据返回 null（调用方显示降级文案）。
 */
function sparklineSvg(values) {
  const W = 300, H = 64, PAD_L = 6, PAD_R = 6, PAD_T = 10, PAD_B = 10;
  const data = values.slice(-SPARK_POINTS).filter((v) => Number.isFinite(v));
  if (data.length < 2) return null;

  const EPS = 1e-30;
  const ys = data.map((v) => Math.log10(Math.max(Math.abs(v), EPS)));
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const span = (yMax - yMin) || 1; // 全平序列也画一条中线
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;
  const x = (i) => PAD_L + (i / (data.length - 1)) * innerW;
  const y = (ly) => PAD_T + (1 - (ly - yMin) / span) * innerH;

  const pts = ys.map((ly, i) => `${x(i).toFixed(2)},${y(ly).toFixed(2)}`).join(' ');
  const lastX = x(data.length - 1);
  const lastY = y(ys[ys.length - 1]);
  const lastVal = data[data.length - 1];
  const lastText = lastVal.toExponential(2);
  const svg = el('svg', {
    class: 'spark-svg',
    viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none',
    role: 'img',
    'aria-label': `残差迷你图（log10 刻度），最近 ${data.length} 点，末值 ${lastText}`,
  });
  const NS = 'http://www.w3.org/2000/svg';
  // 底网格线（顶/底两条，仅结构参考不标数值刻度）
  for (const gy of [PAD_T, H - PAD_B]) {
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', PAD_L); line.setAttribute('x2', W - PAD_R);
    line.setAttribute('y1', gy); line.setAttribute('y2', gy);
    line.setAttribute('class', 'spark-grid');
    svg.append(line);
  }
  const poly = document.createElementNS(NS, 'polyline');
  poly.setAttribute('points', pts);
  poly.setAttribute('class', 'spark-line');
  svg.append(poly);
  const dot = document.createElementNS(NS, 'circle');
  dot.setAttribute('cx', lastX); dot.setAttribute('cy', lastY);
  dot.setAttribute('r', 3);
  dot.setAttribute('class', 'spark-dot');
  svg.append(dot);
  // 末值标注（viewBox 坐标，右侧；preserveAspectRatio=none 下文字可能拉伸，故用小字号+monospace）
  const label = document.createElementNS(NS, 'text');
  label.setAttribute('x', Math.min(lastX + 6, W - 4));
  label.setAttribute('y', Math.max(lastY, PAD_T + 8));
  label.setAttribute('class', 'spark-label');
  label.setAttribute('text-anchor', 'end');
  label.textContent = lastText;
  svg.append(label);
  return svg;
}

function renderSparkline(card, events) {
  const box = el('div', { class: 'spark-box' });
  box.append(el('div', { class: 'spark-title muted-line', text: '残差流（log10 刻度，最近 ' + SPARK_POINTS + ' 点）' }));
  const seq = extractResiduals(events);
  const svg = seq.length >= 2 ? sparklineSvg(seq) : null;
  if (svg) {
    box.append(svg);
  } else {
    box.append(el('p', {
      class: 'spark-empty muted-line',
      text: seq.length === 1 ? '仅 1 个残差采样点，不足以绘图（等待更多心跳）' : '暂无残差流（该作业类型未上报）',
    }));
  }
  card.append(box);
}

/* ---------- Run 卡片 ---------- */
function eventBadge(kind) {
  return badge(EVENT_KINDS.has(kind) ? kind : 'UNKNOWN', {
    zhOverride: EVENT_KINDS.has(kind) ? null : '未知事件',
  });
}

function renderRunCard(entry) {
  const { run, events, taskId } = entry;
  const isFailed = run.execution_state === 'FAILED';
  const isCancelled = run.execution_state === 'CANCELLED';
  const isActive = ACTIVE_EXEC_STATES.has(run.execution_state);

  const card = el('article', { class: 'run-card' });
  if (isFailed) card.classList.add('run-card-failed');
  else if (isCancelled) card.classList.add('run-card-cancelled');
  else if (isActive) card.classList.add('run-card-active');

  const shortId = String(run.run_id || '').slice(0, 14) || '（无 run_id）';

  // ---- 卡头：短 id + variant/condition + 状态徽章 + stage + 心跳 ----
  const hb = lastHeartbeat(events);
  const lastAt = latestEventAt(run, events);
  const stage = lastStage(run, events);
  const heartbeatLost = isActive && (() => {
    const s = secondsSince(hb?.occurred_at ?? lastAt);
    return s === null || s > HEARTBEAT_LOST_AFTER_S;
  })();

  const head = el('div', { class: 'run-head' }, [
    el('div', { class: 'run-head-left' }, [
      el('code', { class: 'run-short-id', text: shortId, title: `${run.run_id}（点击复制全量 ID）` }),
      el('span', { class: 'run-vc', text: `${run.variant_id || '?'} × ${run.condition_id || '?'} · 任务 ${taskId}` }),
    ]),
    el('div', { class: 'run-head-right' }, [
      badge(run.execution_state),
      stage ? el('span', { class: 'stage-chip', text: `阶段：${stage}` }) : el('span', { class: 'muted-line', text: '阶段：无数据' }),
      el('span', {
        class: `hb-chip ${heartbeatLost ? 'hb-lost' : ''}`,
        text: heartbeatLost
          ? `失联：最后心跳 ${hb ? `${secondsSince(hb.occurred_at)}s 前` : '从未收到'}（>${HEARTBEAT_LOST_AFTER_S}s）`
          : `心跳：${hb ? `${secondsSince(hb.occurred_at)}s 前` : '尚未收到'}`,
      }),
    ]),
  ]);
  head.querySelector('.run-short-id').addEventListener('click', () => {
    navigator.clipboard?.writeText(String(run.run_id || '')).catch(() => {});
  });
  card.append(head);

  // 心跳失联条纹告警（文字 + 颜色，双通道）
  if (heartbeatLost) {
    card.append(el('div', {
      class: 'lost-banner', role: 'alert',
      text: `心跳失联告警：Run ${run.run_id} 最后事件距今超过 ${HEARTBEAT_LOST_AFTER_S}s（15s×3），状态未知，显示为失联而非假正常。`,
    }));
  }

  // ---- 残差迷你图（无数据降级文案） ----
  renderSparkline(card, events);

  // ---- 事件时间线：最近 8 条 ----
  card.append(el('h3', { text: `事件时间线（最近 ${RECENT_EVENTS_N} 条，按 event_seq）` }));
  if (events.length) {
    const ul = el('ul', { class: 'event-list' });
    for (const e of events.slice(-RECENT_EVENTS_N)) {
      const li = el('li', { class: 'event-row' }, [
        el('code', { text: `#${e.event_seq ?? '?'}` }),
        document.createTextNode(' '),
        eventBadge(e.kind),
        document.createTextNode(` ${fmtTime(e.occurred_at)}`),
        e.payload?.stage ? document.createTextNode(` · stage=${e.payload.stage}`) : '',
      ]);
      ul.append(li);
    }
    card.append(ul);
    if (events.length > RECENT_EVENTS_N) {
      card.append(el('p', { class: 'muted-line', text: `（共 ${events.length} 条事件，仅展示最近 ${RECENT_EVENTS_N} 条）` }));
    }
  } else {
    card.append(el('p', { class: 'muted-line', text: '无事件数据（该 run 尚未上报 events）。' }));
  }

  // ---- 日志位置（可选字段，缺失降级） ----
  const logPath = hb?.payload?.log_path || hb?.payload?.log_location || null;
  card.append(el('p', { class: 'log-line' }, [
    document.createTextNode('日志位置：'),
    logPath ? el('code', { text: logPath }) : el('span', { class: 'muted-line', text: '（未上报）' }),
  ]));

  return card;
}

/* ---------- 主区域 ---------- */
function renderRunCards() {
  const box = $('run-cards');
  box.replaceChildren();

  // tasks 接口错误（非离线）：整页降级块
  if (state.tasks && state.tasks.__error) {
    box.append(el('p', { class: 'muted-line', text: '任务列表接口未就绪，监控台无法聚合运行中作业。' }));
    box.append(errorBlock(state.tasks.__error));
    return;
  }

  const actives = activeRuns();
  if (actives.length) {
    box.append(el('p', { class: 'summary-line', text: `进行中的 Run：${actives.length} 个。失败/取消的历史 Run 不在此列（见下方折叠区）。` }));
    for (const entry of actives) box.append(renderRunCard(entry));
  } else {
    box.append(el('p', { class: 'empty-line', text: '当前没有进行中的仿真作业。' }));
  }

  // 逐任务 runs 读取失败提示（保留缓存态时说明数据可能过期）
  if (state.taskErrors.size) {
    const warn = el('div', { class: 'task-errors' });
    warn.append(el('p', { class: 'warn-line', text: `以下 ${state.taskErrors.size} 个任务的 runs 读取失败，卡片为上一轮缓存（可能过期，以时间戳为准）：` }));
    const ul = el('ul', { class: 'task-error-list' });
    for (const [tid, err] of state.taskErrors) {
      ul.append(el('li', {}, [
        el('code', { text: tid }),
        document.createTextNode(` — ${err.code || 'ERROR'}：${err.message || '未知错误'}`),
      ]));
    }
    warn.append(ul);
    box.append(warn);
  }

  // 空态补充：最近完成任务短列表（可折叠）
  const finished = recentFinishedRuns(5);
  if (finished.length) {
    const details = el('details', { class: 'finished-details' });
    details.append(el('summary', { text: `最近完成的 Run（${finished.length} 条，点击展开）` }));
    const ul = el('ul', { class: 'finished-list' });
    for (const { run, events, taskId } of finished) {
      ul.append(el('li', {}, [
        el('code', { text: String(run.run_id || '').slice(0, 14) }),
        document.createTextNode(' '),
        badge(run.execution_state),
        document.createTextNode(` ${run.variant_id || '?'}×${run.condition_id || '?'} · 任务 ${taskId} · 结束于 ${fmtTime(latestEventAt(run, events))}`),
      ]));
    }
    details.append(ul);
    box.append(details);
  }
}

/* ---------- 渲染入口 ---------- */
function renderAll() {
  renderTopbar();
  renderServiceStatus();
  renderOffline();
  renderPollStatus();
  renderRunCards();
}

/* ---------- 轮询（3s；document.hidden 暂停省资源） ---------- */
function setupPolling() {
  setInterval(() => {
    if (document.hidden) { renderPollStatus(); return; }
    pollAll();
  }, POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !state.loading) pollAll(); // 回到页面立即刷一次
    renderPollStatus();
  });
}

/* ---------- 事件绑定 ---------- */
$('btn-refresh').addEventListener('click', () => pollAll());

/* ---------- 启动 ---------- */
renderAll();
pollAll();
setupPolling();
