/**
 * dsh-sim · panels/executor/app.js（WP-12 执行任务工作台）
 * 定义书 §执行工作台：交互定义 五区域实现。
 *
 * 降级纪律：工程服务（Agent C 并行开发中）未就绪时，各区域显示降级说明，
 * 绝不静默空白；evidence_mode 未知时顶栏显示 UNKNOWN 徽章。
 *
 * 面板期望但 openapi.v0.1 暂未定义的读取端点（交接给 Agent E 补契约）：
 *   GET /tasks                       列表（filter 查询参数可选）
 *   GET /tasks/{task_id}             Task 对象
 *   GET /tasks/{task_id}/revisions/latest   当前 TaskRevision
 *   GET /tasks/{task_id}/runs        { items: Run[] }（矩阵渲染硬依赖）
 *   GET /tasks/{task_id}/preparations/latest  最新 Preparation
 *   GET /tasks/{task_id}/bundles/latest       最新 Bundle
 * 以上任一 404/离线 → 对应区域降级，不影响其他区域。
 */
import { apiGet, apiPost } from '../shared/api.js';
import {
  el, esc, badge, evidenceModeBadge, digestShort, traceRow,
  blockerList, offlineBanner, errorBlock, fmtTime, secondsSince,
} from '../shared/components.js';

// 开发模式，生产走受信会话。角色必须使用服务端合法枚举（EXECUTOR 等；
// 旧值 ENGINEER 不是合法角色，authorizeRuns 会被 403 拒绝）。
// 项目不在此硬编码：shared/api.js 的 devIdentityHeaders 默认 dshsim.projects=proj_a
// （与 MCP DSH_SIM_AGENT_PROJECTS 默认一致，同一专用测试项目），需要时用
// localStorage 覆盖，显式配置优先于默认值。
const IDENTITY = { subject: 'dev-executor', roles: 'EXECUTOR' };
const HEARTBEAT_LOST_AFTER_S = 45; // 心跳 15s × 3 次失联判定（定义书 §并发、断线、取消与补算规则）
const ACTIVE_EXEC_STATES = new Set(['QUEUED', 'WAITING_RESOURCE', 'LEASED', 'STARTING', 'RUNNING', 'CANCELLING', 'COLLECTING']);

const state = {
  taskId: new URLSearchParams(location.search).get('task') || null,
  filter: 'mine',
  task: null,
  revision: null,
  runs: [],
  preparation: null,
  bundle: null,
  claims: [],
  metrics: null,
  authorizations: [],
  offline: false,
  selectedRunId: null,
  pollTimer: null,
};

/** 当前修订仍有效的授权（交接恢复用）；null 表示没有可复用授权。 */
function activeAuthorization() {
  const t = state.task;
  if (!t || t.__error) return null;
  return (state.authorizations || []).find((a) =>
    a.validity === 'CURRENT' && !a.revoked_at && a.revision === t.current_revision
  ) || null;
}

const $ = (id) => document.getElementById(id);

/* ---------- 通用数据获取（降级返回 null） ---------- */
async function tryGet(path, areaLabel) {
  const r = await apiGet(path, { identity: IDENTITY });
  if (r.offline) {
    state.offline = true;
    return null;
  }
  if (r.error) {
    console.warn(`[executor] ${areaLabel} 读取失败`, r.error);
    return { __error: r.error };
  }
  return r.data;
}

/* ---------- 顶栏 ---------- */
function renderTopbar() {
  const slot = $('evidence-mode-slot');
  slot.replaceChildren();
  const mode = state.offline
    ? 'UNKNOWN'
    : (state.task?.evidence_mode || state.preparation?.evidence_mode || state.bundle?.evidence_mode || 'UNKNOWN');
  slot.append(evidenceModeBadge(mode));
  if (mode === 'MOCK') {
    slot.append(el('span', { class: 'mock-watermark', text: 'MOCK' }));
  }
}

function renderOffline() {
  const slot = $('offline-slot');
  slot.replaceChildren();
  if (state.offline) {
    slot.append(offlineBanner('无法连接工程服务（127.0.0.1:8600）。面板显示为空态/缓存态，不代表真实任务状态。连接恢复后点击"刷新"。'));
  }
}

/* ---------- 区域 1：任务列表 ---------- */
const FILTERS = {
  mine:    { label: '需要我处理', match: (t) => ['DRAFT', 'PREPARING', 'READY', 'CHANGES_REQUESTED'].includes(t.task_state) },
  active:  { label: '运行中',     match: (t) => ['AUTHORIZED', 'ACTIVE'].includes(t.task_state) },
  review:  { label: '待审查',     match: (t) => ['READY_FOR_REVIEW', 'IN_REVIEW'].includes(t.task_state) },
  history: { label: '历史',       match: (t) => ['ACCEPTED', 'REJECTED'].includes(t.task_state) },
};

async function loadTaskList() {
  const box = $('task-list');
  box.replaceChildren(el('p', { class: 'muted-line', text: '加载中…' }));
  const data = await tryGet('/tasks', '任务列表');
  box.replaceChildren();
  if (state.offline) {
    box.append(el('p', { class: 'muted-line', text: '服务离线，列表不可用；可在下方手工输入任务 ID。' }));
    return;
  }
  if (!data || data.__error) {
    box.append(el('p', { class: 'muted-line', text: `任务列表接口未就绪（${data?.__error?.code || '无响应'}）；请在下方手工输入任务 ID 加载。` }));
    if (data?.__error) box.append(traceRow(data.__error.trace_id));
    return;
  }
  const items = (data.items || []).filter(FILTERS[state.filter].match);
  if (!items.length) {
    box.append(el('p', { class: 'muted-line', text: `「${FILTERS[state.filter].label}」下暂无任务。` }));
    return;
  }
  for (const t of items) {
    const item = el('button', { class: 'task-item', type: 'button' }, [
      el('div', { class: 'task-item-id', text: t.task_id }),
      el('div', { class: 'task-item-meta' }, [
        el('span', { text: `R${t.current_revision}` }),
        badge(t.task_state),
      ]),
    ]);
    if (t.task_id === state.taskId) item.classList.add('selected');
    item.addEventListener('click', () => loadTask(t.task_id));
    box.append(item);
  }
}

/* ---------- 区域 2：主任务卡 ---------- */
function renderTaskCard() {
  const body = $('task-card-body');
  body.replaceChildren();
  const t = state.task;
  if (!t) { body.append(el('p', { class: 'muted-line', text: '尚未加载任务。' })); return; }
  if (t.__error) { body.append(errorBlock(t.__error)); return; }

  const spec = state.revision && !state.revision.__error ? state.revision.spec : null;
  const head = el('div', { class: 'task-head' }, [
    el('div', {}, [
      el('strong', { class: 'task-title', text: spec?.title || spec?.objective || t.task_id }),
      el('div', { class: 'task-sub' }, [
        el('span', { text: `任务 ${t.task_id} · 修订 R${t.current_revision} · 创建人 ${esc(t.owner_id || '—')}` }),
      ]),
    ]),
    el('div', { class: 'task-head-badges' }, [badge(t.task_state), badge(t.review_state)]),
  ]);
  body.append(head);

  const facts = el('dl', { class: 'fact-grid' }, [
    el('dt', { text: '用途' }), el('dd', { text: `${t.purpose || 'design_screening'}（内部方案筛选）` }),
    el('dt', { text: '方法包' }), el('dd', { text: spec?.method?.package_id ? `${spec.method.package_id}@${spec.method.package_version || '?'}` : '（修订未就绪）' }),
    el('dt', { text: '输入来源' }), el('dd', { text: (state.revision?.source_refs || []).join('、') || '（未提供）' }),
    el('dt', { text: 'spec 摘要' }), el('dd', {}, [digestShort(state.revision?.spec_sha256)]),
  ]);
  body.append(facts);

  if (spec?.objective) body.append(el('p', { class: 'objective', text: `目标：${spec.objective}` }));

  // A/B 工况矩阵：variants × conditions，每格 run 状态徽章
  renderMatrix(body, spec);

  if (Array.isArray(t.blockers) && t.blockers.length) {
    body.append(el('h3', { text: '任务阻塞项' }));
    body.append(blockerList(t.blockers));
  }
}

function renderMatrix(container, spec) {
  const wrap = el('div', { class: 'matrix-wrap' });
  wrap.append(el('h3', { text: '工况矩阵（方案 × 工况，每格为逻辑 Run）' }));
  if (!spec || !Array.isArray(spec.variants) || !Array.isArray(spec.conditions)) {
    wrap.append(el('p', { class: 'muted-line', text: '修订内容未就绪，矩阵无法渲染（NOT_RUN）。' }));
    container.append(wrap);
    return;
  }
  const table = el('table', { class: 'grid matrix' });
  const thead = el('tr', {}, [el('th', { text: '方案 \\ 工况' }),
    ...spec.conditions.map((c) => el('th', { text: c.condition_id + (c.label ? ` ${c.label}` : '') }))]);
  table.append(thead);
  for (const v of spec.variants) {
    const tr = el('tr', {}, [el('th', { text: v.variant_id + (v.label ? ` ${v.label}` : '') })]);
    for (const c of spec.conditions) {
      const run = state.runs.find((r) => r.variant_id === v.variant_id && r.condition_id === c.condition_id);
      const td = el('td', { class: 'matrix-cell' });
      if (run && !run.__error) {
        td.append(
          el('div', { class: 'run-id-line' }, [el('code', { text: run.run_id })]),
          el('div', {}, [badge(run.execution_state), badge(run.numerical_state), badge(run.applicability_state)]),
        );
        td.addEventListener('click', () => { state.selectedRunId = run.run_id; renderRunPane(); });
        td.classList.add('clickable');
        td.title = '点击在运行区域查看该 Run';
      } else {
        td.append(badge('NOT_RUN'));
        td.append(el('div', { class: 'muted-line', text: '尚无 Run 记录' }));
      }
      tr.append(td);
    }
    table.append(tr);
  }
  wrap.append(table);
  wrap.append(el('p', { class: 'legend', text: '说明：执行完成(SUCCEEDED) ≠ 数值通过(PASS)；数值/适用性各自独立判定，不以单一绿勾代替。' }));
  container.append(wrap);
}

/* ---------- 区域 3：确认区域 ---------- */
function diffStatus(d) {
  const s = (d.status || d.result || '').toUpperCase();
  if (['MATCH', 'OK', 'CONSISTENT', '一致'].includes(s)) return 'match';
  if (s) return 'diff';
  // 无状态字段时：有 requested/readback 且相等视为一致，否则按差异处理（保守）
  if (d.requested !== undefined && d.readback !== undefined) {
    return String(d.requested) === String(d.readback) ? 'match' : 'diff';
  }
  return 'diff';
}

function renderConfirmPane() {
  const body = $('confirm-body');
  body.replaceChildren();
  const p = state.preparation;
  if (!p) { body.append(el('p', { class: 'muted-line', text: '无准备数据（服务离线或未发起准备）。' })); return; }
  if (p.__error) { body.append(errorBlock(p.__error)); return; }

  body.append(el('div', { class: 'prep-meta' }, [
    el('div', {}, [document.createTextNode('prepared 摘要：'), digestShort(p.prepared_digest)]),
    el('div', { class: 'muted-line', text: `准备 ID ${p.preparation_id} · 修订 R${p.revision} · 软件构建 ${esc(p.software_build || '（未知）')} · 适配器 ${esc(p.adapter_build || '（未知）')}` }),
    el('div', { class: 'muted-line', text: '工程师确认的是该真实产物的摘要，而非模型计划写入的文字（定义书 §端到端业务流程）。' }),
  ]));

  // 差异对照表
  const diffs = Array.isArray(p.differences) ? p.differences : [];
  if (diffs.length) {
    const table = el('table', { class: 'grid diff-table' });
    table.append(el('tr', {}, ['字段', '边界身份', '申请值', '真实回读值', '单位', '差异'].map((h) => el('th', { text: h }))));
    for (const d of diffs) {
      const st = diffStatus(d);
      table.append(el('tr', { class: st === 'diff' ? 'row-diff' : '' }, [
        el('td', { text: d.field || d.path || '—' }),
        el('td', { text: d.boundary || d.role_id || d.boundary_role || '—' }),
        el('td', { text: fmtVal(d.requested ?? d.applied) }),
        el('td', { text: fmtVal(d.readback ?? d.actual) }),
        el('td', { text: d.unit || '—' }),
        el('td', {}, [badge(st === 'match' ? 'PASS' : 'FAIL', { zhOverride: st === 'match' ? '一致' : '有差异' })]),
      ]));
    }
    body.append(table);
  } else {
    body.append(el('p', { class: 'ok-line', text: '回读无差异记录（或差异接口未返回明细）。' }));
  }

  // 阻塞项
  const blockers = Array.isArray(p.blockers) ? p.blockers : [];
  body.append(el('h3', { text: '阻塞项（未清时运行按钮禁用）' }));
  body.append(blockerList(blockers));

  // 主按钮：仅确认并授权（不提交）。首次 submit_runs 必须由 AGENT runner 经
  // 工程 MCP 的 submit_runs 工具完成（定义书 §人工授权；面板不代跑作业提交）。
  // 旧"确认并运行"三连按钮已移除：授权与提交混在同一动作会让面板提交被记成
  // runner 提交，破坏人工授权/模型执行的职责分离。
  const unresolved = diffs.some((d) => diffStatus(d) === 'diff') || blockers.length > 0;
  const readyState = state.task && !state.task.__error && state.task.task_state === 'READY';
  const activeAuth = activeAuthorization();

  // 交接恢复块：授权后关闭弹窗或刷新，从这里读回 authorization_id + prepared_digest。
  if (activeAuth) {
    body.append(el('div', { class: 'prep-meta', id: 'active-authorization' }, [
      el('h3', { text: '当前有效授权（交接恢复；勿重复授权）' }),
      el('div', {}, [document.createTextNode('authorization_id：'), el('code', { text: activeAuth.authorization_id })]),
      el('div', {}, [document.createTextNode('prepared_digest：'), digestShort(activeAuth.prepared_digest)]),
      el('div', { class: 'muted-line', text: `修订 R${activeAuth.revision} · 授权人 ${activeAuth.authorized_by} · 有效性 ${activeAuth.validity}` }),
      el('p', { class: 'muted-line', text: '首次提交必须由 AGENT runner 经工程 MCP 的 submit_runs 使用以上 authorization_id + prepared_digest 完成；本面板不再重复生成授权。' }),
    ]));
  }

  const btnRow = el('div', { class: 'btn-row' });
  const btnRun = el('button', {
    class: 'btn btn-primary', type: 'button',
    text: '确认并授权（不提交）',
    title: '绑定 prepared 摘要的人工授权；提交由 AGENT runner 经 MCP 完成',
  });
  btnRun.disabled = !(readyState && !unresolved && !state.offline) || !!activeAuth;
  if (activeAuth) {
    btnRun.title = '当前修订已有有效授权（见上方交接恢复块）；不重复授权';
  } else if (btnRun.disabled) {
    btnRun.title = '差异未清 / 存在阻塞 / 任务未到 READY / 服务离线时禁用';
  }
  btnRun.addEventListener('click', onAuthorizeOnly);
  const btnBack = el('button', { class: 'btn', type: 'button', text: '返回修改' });
  btnBack.addEventListener('click', () => showModal('返回修改', [
    el('p', { text: '修改边界、几何、方法或预算属于新修订：请回到 DSH 对话由工程师创建新 TaskRevision（修订发布后旧授权自动失效，需重新准备与确认）。' }),
    el('p', { class: 'muted-line', text: '本面板不提供原位修改输入的入口。' }),
  ]));
  btnRow.append(btnRun, btnBack);
  body.append(btnRow);

  if (btnRun.disabled) {
    const reasons = [];
    if (state.offline) reasons.push('工程服务离线');
    if (activeAuth) reasons.push('当前修订已有有效授权（见上方交接恢复块），不重复授权');
    if (!readyState) reasons.push(`任务阶段为 ${state.task?.task_state || '未知'}`);
    if (!activeAuth && !readyState && state.task?.task_state !== 'READY') reasons.push('任务未到 READY');
    if (diffs.some((d) => diffStatus(d) === 'diff')) reasons.push('存在未清申请/回读差异');
    if (blockers.length) reasons.push(`存在 ${blockers.length} 项阻塞（见上方清单，含责任人与可采取动作）`);
    body.append(el('ul', { class: 'disable-reasons' }, reasons.map((r) => el('li', { text: r }))));
  }
}

function fmtVal(v) {
  if (v === undefined || v === null) return '（缺失 null）';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* 确认并授权（仅授权，不提交）：一次性人工确认 → 授权 → 停止（FR-08/FR-09）。
 * 授权产出 authorization_id + prepared_digest；首次 submit_runs 必须由 AGENT
 * runner 经工程 MCP 完成，本面板不代替模型提交作业（职责分离）。
 */
async function onAuthorizeOnly() {
  const p = state.preparation;
  const t = state.task;
  const budget = state.revision?.spec?.execution_budget || {};
  const ok = await showModal('人工运行授权（仅授权，不提交）', [
    el('p', { text: '授权对象为以下真实准备产物摘要；确认后任务进入 AUTHORIZED，但不会入队任何作业。' }),
    el('p', { text: `任务 ${t.task_id} · 修订 R${t.current_revision} · 用途 ${t.purpose}` }),
    el('p', { text: `准备 ${p.preparation_id}` }),
    el('div', { class: 'confirm-digest' }, [document.createTextNode('prepared_digest：'), digestShort(p.prepared_digest)]),
    el('p', { text: `执行预算：${JSON.stringify(budget)}` }),
    el('p', { class: 'warn-line', text: 'Agent 不能代替本确认；确认凭据一次性消费。首次提交由 AGENT runner 经工程 MCP submit_runs 使用本授权完成。' }),
  ], { confirmText: '确认并授权（不提交）', danger: false });
  if (!ok) return;

  // 1) createHumanConfirmation：target_id 必须是 task_id（服务端 consume 校验
  //    authorizeRuns 绑定 target_id=task_id；旧实现误传 preparation_id 导致 403）。
  const conf = await apiPost('/confirmations', {
    action: 'authorizeRuns', target_id: t.task_id, target_digest: p.prepared_digest,
  }, { identity: IDENTITY, actionPrefix: 'confirm' });
  if (conf.offline) { state.offline = true; renderAll(); return; }
  if (conf.error) { showModal('确认凭据签发失败', [errorBlock(conf.error)]); return; }
  const confirmationId = conf.data.confirmation_id;

  // 2) authorizeRuns：到这里为止；不调用 submissions。
  const auth = await apiPost(`/tasks/${t.task_id}/authorizations`, {
    revision: t.current_revision,
    preparation_id: p.preparation_id,
    prepared_digest: p.prepared_digest,
    execution_budget: budget,
    confirmation_id: confirmationId,
  }, { identity: IDENTITY, actionPrefix: 'authorize' });
  if (auth.error) { showModal('授权失败', [errorBlock(auth.error)]); return; }

  showModal('已授权（未提交）', [
    el('p', { text: `授权已生效，任务进入 AUTHORIZED。authorization_id：${auth.data.authorization_id}` }),
    el('div', { class: 'confirm-digest' }, [document.createTextNode('prepared_digest：'), digestShort(p.prepared_digest)]),
    el('p', { text: '本面板没有入队任何作业。首次提交必须由 AGENT runner 经工程 MCP 的 submit_runs 工具，使用上述 authorization_id + prepared_digest 完成。' }),
    el('p', { class: 'muted-line', text: '关闭本弹窗或刷新页面后，确认区域的"当前有效授权（交接恢复）"块会读回同一 authorization_id + prepared_digest，不会重复授权。' }),
    el('p', { class: 'muted-line', text: '提交后可在本面板运行区域查看真实进度；也可在此请求取消。' }),
  ]);
  await loadTask(t.task_id);
}

/* ---------- 区域 4：运行区域 ---------- */
function renderRunPane() {
  const body = $('run-body');
  body.replaceChildren();
  if (!state.runs.length) {
    body.append(el('p', { class: 'muted-line', text: '尚无 Run（未提交或 runs 接口未就绪）。' }));
    return;
  }
  const run = state.runs.find((r) => r.run_id === state.selectedRunId) || state.runs[0];
  state.selectedRunId = run.run_id;

  // LOST 醒目横幅
  if (run.execution_state === 'LOST') {
    body.append(el('div', { class: 'lost-banner', role: 'alert', text: `Run ${run.run_id} 状态 LOST：执行状态未知，已冻结重派，需人工核实现场后方可处理（定义书 §作业生命周期）。` }));
  }

  // Run 选择
  const sel = el('select', { class: 'run-select' });
  for (const r of state.runs) {
    const opt = el('option', { value: r.run_id, text: `${r.run_id}（${r.variant_id}×${r.condition_id}）` });
    if (r.run_id === run.run_id) opt.selected = true;
    sel.append(opt);
  }
  sel.addEventListener('change', () => { state.selectedRunId = sel.value; renderRunPane(); });
  body.append(el('label', { class: 'field' }, [el('span', { text: '选择 Run' }), sel]));

  // 多维状态
  body.append(el('div', { class: 'run-badges' }, [
    badge(run.execution_state), badge(run.numerical_state), badge(run.applicability_state),
  ]));

  // 真实阶段 / 等待原因 / 最后心跳
  const events = Array.isArray(run.events) ? [...run.events].sort((a, b) => a.event_seq - b.event_seq) : [];
  const lastHb = [...events].reverse().find((e) => e.kind === 'HEARTBEAT');
  const lastEvt = events[events.length - 1];
  const waitReason = run.execution_state === 'WAITING_RESOURCE'
    ? (lastEvt?.payload?.reason || lastEvt?.payload?.wait_reason || '等待资源/许可证（原因未随事件提供）')
    : null;

  const facts = el('dl', { class: 'fact-grid' });
  facts.append(el('dt', { text: '真实阶段' }), el('dd', { text: lastHb?.payload?.stage || lastEvt?.payload?.stage || run.execution_state }));
  if (waitReason) facts.append(el('dt', { text: '等待原因' }), el('dd', { text: waitReason }));
  facts.append(el('dt', { text: '最后心跳' }), el('dd', { text: fmtTime(lastHb?.occurred_at) }));
  facts.append(el('dt', { text: '当前 attempt' }), el('dd', { text: run.current_attempt_id || '（无）' }));
  facts.append(el('dt', { text: '日志位置' }), el('dd', { text: lastHb?.payload?.log_path || '（未上报）' }));
  body.append(facts);

  // 心跳失联提示（>45s 无心跳）
  if (ACTIVE_EXEC_STATES.has(run.execution_state)) {
    const since = secondsSince(lastHb?.occurred_at);
    if (since === null || since > HEARTBEAT_LOST_AFTER_S) {
      body.append(el('div', { class: 'lost-banner', role: 'alert', text: `最后心跳 ${since === null ? '从未收到' : `${since} 秒前`}（超过 ${HEARTBEAT_LOST_AFTER_S}s 阈值）：状态未知，显示为失联告警而非假正常。` }));
    } else {
      body.append(el('p', { class: 'muted-line', text: `心跳正常（${since}s 前）。界面状态延迟目标 ≤30s，过期数据以时间戳为准。` }));
    }
  }

  // 事件流（简短）
  if (events.length) {
    body.append(el('h3', { text: '事件（按序，增量读取）' }));
    const list = el('ul', { class: 'event-list' });
    for (const e of events.slice(-12)) {
      list.append(el('li', {}, [
        el('code', { text: `#${e.event_seq}` }),
        document.createTextNode(` ${e.kind} · ${fmtTime(e.occurred_at)}`),
        e.payload?.message ? document.createTextNode(` · ${e.payload.message}`) : '',
      ]));
    }
    body.append(list);
  }

  // 取消按钮（独立二次确认）
  if (ACTIVE_EXEC_STATES.has(run.execution_state) && run.execution_state !== 'CANCELLING') {
    const btnCancel = el('button', { class: 'btn btn-danger', type: 'button', text: '请求取消该 Run' });
    btnCancel.addEventListener('click', () => onCancelRun(run));
    body.append(el('div', { class: 'btn-row' }, [btnCancel]));
    body.append(el('p', { class: 'muted-line', text: '取消为异步请求；全部受控子进程退出后才显示 CANCELLED，无法证实退出标记 LOST。' }));
  }
}

async function onCancelRun(run) {
  const ok = await showModal('取消确认（二次确认）', [
    el('p', { text: `确定请求取消 Run ${run.run_id}（${run.variant_id}×${run.condition_id}）？` }),
    el('p', { class: 'warn-line', text: '取消请求入库 ≠ 已停止；系统将先尝试软件允许的停止，再受控终止进程组。' }),
  ], { confirmText: '确认请求取消', danger: true });
  if (!ok) return;
  const r = await apiPost(`/runs/${run.run_id}/cancel`, { reason: '工程师在面板请求取消' }, { identity: IDENTITY, actionPrefix: 'cancel' });
  if (r.error) { showModal('取消请求失败', [errorBlock(r.error)]); return; }
  await refreshRuns();
  renderRunPane();
}

/* ---------- 区域 5：结果区域 ---------- */
function renderResultPane() {
  const body = $('result-body');
  body.replaceChildren();
  const b = state.bundle;
  if (!b) { body.append(el('p', { class: 'muted-line', text: '尚无证据包（未构建或 bundles 接口未就绪）。' })); return; }
  if (b.__error) { body.append(errorBlock(b.__error)); return; }

  body.append(el('div', { class: 'prep-meta' }, [
    el('div', {}, [document.createTextNode('bundle 摘要：'), digestShort(b.bundle_digest)]),
    el('div', { class: 'muted-line' }, [document.createTextNode(`修订 R${b.revision} · 有效性 `), badge(b.validity)]),
  ]));

  // 先：对设计问题的回答 + 限制
  body.append(el('h3', { text: '对设计问题的回答' }));
  const confirmed = state.claims.filter((c) => c.state === 'CONFIRMED');
  const drafts = state.claims.filter((c) => c.state === 'DRAFT');
  if (confirmed.length || drafts.length) {
    const ul = el('ul', { class: 'claim-list' });
    for (const c of confirmed) ul.append(el('li', {}, [badge('PASS', { zhOverride: '已确认' }), document.createTextNode(' ' + c.text), el('span', { class: 'muted-line', text: `（metric ${c.metric_id} · artifact ${c.artifact_id}）` })]));
    for (const c of drafts) ul.append(el('li', { class: 'claim-draft' }, [badge('NOT_CHECKED', { zhOverride: '草稿(AGENT)' }), document.createTextNode(' ' + c.text)]));
    body.append(ul);
  } else {
    body.append(el('p', { class: 'muted-line', text: '无已确认结论（claims 接口未就绪或全部未完成）；未完成的结论不得当作设计结论。' }));
  }
  const lim = b.limitations || state.task?.limitations;
  body.append(el('p', { class: 'warn-line', text: `限制：${lim || '仅限内部方案筛选用途；不构成适航符合性结论。'}` }));

  // ★ 云图实况：manifest 中的 PNG 以内联图像展示（真实 STAR-CCM+ batch 渲染，
  //   经 artifact API 受控出图——不是截图，哈希可追溯）
  const pngs = (Array.isArray(b.manifest) ? b.manifest : []).filter((m) => /\.png$/i.test(m.logical_path || ''));
  if (pngs.length) {
    body.append(el('h3', { text: '云图实况（STAR-CCM+ batch 渲染 · 点击放大）' }));
    const grid = el('div', { class: 'scene-grid' });
    const baseUrl = new URLSearchParams(location.search).get('api') || localStorage.getItem('dshsim.api_base') || 'http://127.0.0.1:8600/api/v1';
    for (const m of pngs) {
      const url = `${baseUrl.replace(/\/$/, '')}/artifacts/${encodeURIComponent(m.artifact_id)}/content`;
      const fig = el('figure', { class: 'scene-fig' });
      const img = el('img', { src: url, alt: m.logical_path, loading: 'lazy' });
      img.addEventListener('click', () => window.open(url, '_blank'));
      fig.append(img, el('figcaption', {}, [
        document.createTextNode(sceneLabel(m.logical_path)),
        document.createTextNode(' '),
        digestShort(m.sha256),
      ]));
      grid.append(fig);
    }
    body.append(grid);
  }

  // 再：指标表
  body.append(el('h3', { text: '指标（定义随数据保存，缺值为 null 不填 0）' }));
  if (state.metrics && Array.isArray(state.metrics.items) && state.metrics.items.length) {
    const table = el('table', { class: 'grid' });
    table.append(el('tr', {}, ['指标', '方案/工况', '数值', '单位', '来源 artifact'].map((h) => el('th', { text: h }))));
    for (const m of state.metrics.items) {
      table.append(el('tr', {}, [
        el('td', { text: m.metric_id || '—' }),
        el('td', { text: `${m.variant_id || ''}×${m.condition_id || ''}` }),
        el('td', { text: m.value === null || m.value === undefined ? 'null（缺失）' : String(m.value) }),
        el('td', { text: m.unit || '—' }),
        el('td', {}, [artifactLink(m.artifact_id)]),
      ]));
    }
    body.append(table);
  } else {
    body.append(el('p', { class: 'muted-line', text: '指标接口未就绪或尚无指标（NOT_RUN）。' }));
  }

  // 原始文件链接
  body.append(el('h3', { text: '原始证据文件（只读）' }));
  const manifest = Array.isArray(b.manifest) ? b.manifest : [];
  const csvs = manifest.filter((m) => /\.csv$/i.test(m.logical_path || ''));
  const sims = manifest.filter((m) => /\.sim$/i.test(m.logical_path || ''));
  const others = manifest.filter((m) => !/\.(csv|sim)$/i.test(m.logical_path || ''));
  const mkList = (items, label) => {
    if (!items.length) return el('p', { class: 'muted-line', text: `${label}：无。` });
    const ul = el('ul', {});
    for (const m of items) {
      ul.append(el('li', {}, [
        artifactLink(m.artifact_id, m.logical_path),
        document.createTextNode(' '),
        digestShort(m.sha256),
      ]));
    }
    return el('div', {}, [el('strong', { text: label }), ul]);
  };
  body.append(mkList(csvs, '原始 CSV'), mkList(sims, '.sim 结果文件'), mkList(others, '其他清单项'));
}

function artifactLink(artifactId, label) {
  if (!artifactId) return el('span', { class: 'muted-line', text: '（无 artifact）' });

// 云图 logical_path → 中文标签
function sceneLabel(logicalPath) {
  const map = {
    pressure: '压力云图', stream: '流线', lic: 'LIC 纹理', vector: '速度矢量',
    density: '密度', temperature: '温度', mesh: '网格', geometry: '几何',
    velocity: '速度幅值', section: '截面',
  };
  const low = (logicalPath || '').toLowerCase();
  for (const [k, v] of Object.entries(map)) if (low.includes(k)) return v;
  return logicalPath || '场景渲染';
}
  const base = new URLSearchParams(location.search).get('api') || localStorage.getItem('dshsim.api_base') || 'http://127.0.0.1:8600/api/v1';
  return el('a', {
    href: `${base.replace(/\/$/, '')}/artifacts/${encodeURIComponent(artifactId)}/content`,
    text: label || `artifact ${artifactId}`,
    target: '_blank', rel: 'noopener',
  });
}

/* ---------- 模态 ---------- */
function showModal(title, bodyNodes, { confirmText = null, danger = false } = {}) {
  return new Promise((resolve) => {
    const root = $('modal-root');
    root.replaceChildren();
    const close = (val) => { root.replaceChildren(); resolve(val); };
    const btns = el('div', { class: 'btn-row' });
    if (confirmText) {
      const ok = el('button', { class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`, type: 'button', text: confirmText });
      ok.addEventListener('click', () => close(true));
      const no = el('button', { class: 'btn', type: 'button', text: '取消' });
      no.addEventListener('click', () => close(false));
      btns.append(no, ok);
    } else {
      const ok = el('button', { class: 'btn', type: 'button', text: '关闭' });
      ok.addEventListener('click', () => close(true));
      btns.append(ok);
    }
    const modal = el('div', { class: 'modal', role: 'dialog', 'aria-label': title }, [
      el('h2', { text: title }), ...[].concat(bodyNodes), btns,
    ]);
    const mask = el('div', { class: 'modal-mask' }, [modal]);
    mask.addEventListener('click', (e) => { if (e.target === mask) close(false); });
    root.append(mask);
  });
}

/* ---------- 数据加载 ---------- */
async function refreshRuns() {
  const data = await tryGet(`/tasks/${state.taskId}/runs`, 'Run 列表');
  if (data && !data.__error) state.runs = Array.isArray(data.items) ? data.items : [];
}

async function loadTask(taskId) {
  state.taskId = taskId;
  state.task = state.revision = state.preparation = state.bundle = null;
  state.runs = []; state.claims = []; state.metrics = null;
  state.authorizations = [];
  state.offline = false;
  renderAll();

  state.task = await tryGet(`/tasks/${taskId}`, '任务');
  if (!state.task || state.task.__error) { renderAll(); return; }

  const [rev, prep, bundle, auths] = await Promise.all([
    tryGet(`/tasks/${taskId}/revisions/latest`, '修订'),
    tryGet(`/tasks/${taskId}/preparations/latest`, '准备'),
    tryGet(`/tasks/${taskId}/bundles/latest`, '证据包'),
    tryGet(`/tasks/${taskId}/authorizations`, '授权（交接恢复）'),
  ]);
  state.revision = rev;
  state.preparation = prep;
  state.bundle = bundle;
  if (auths && !auths.__error) state.authorizations = Array.isArray(auths.items) ? auths.items : [];
  await refreshRuns();

  if (bundle && !bundle.__error) {
    const [claims, metrics] = await Promise.all([
      tryGet(`/bundles/${bundle.bundle_id}/claims`, 'Claim'),
      tryGet(`/bundles/${bundle.bundle_id}/metrics`, '指标'),
    ]);
    if (claims && !claims.__error) state.claims = Array.isArray(claims.items) ? claims.items : [];
    if (metrics && !metrics.__error) state.metrics = metrics;
  }
  renderAll();
  setupPolling();
}

function setupPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  const active = state.task && !state.task.__error && ['AUTHORIZED', 'ACTIVE'].includes(state.task.task_state);
  if (!active) return;
  // 2—5s 抖动的有界轮询（定义书 §并发、断线、取消与补算规则）
  const tick = async () => {
    if (document.hidden) return;
    await refreshRuns();
    renderRunPane();
    renderTaskCard();
  };
  state.pollTimer = setInterval(tick, 4000 + Math.round(Math.random() * 2000));
}

/* ---------- 渲染入口 ---------- */
function renderAll() {
  renderTopbar();
  renderOffline();
  renderTaskCard();
  renderConfirmPane();
  renderRunPane();
  renderResultPane();
}

/* ---------- 事件绑定 ---------- */
for (const btn of document.querySelectorAll('.filter-btn')) {
  btn.addEventListener('click', () => {
    state.filter = btn.dataset.filter;
    document.querySelectorAll('.filter-btn').forEach((b) => b.classList.toggle('active', b === btn));
    loadTaskList();
  });
}
document.querySelector('.filter-btn[data-filter="mine"]').classList.add('active');
$('btn-load-task').addEventListener('click', () => {
  const id = $('manual-task-id').value.trim();
  if (id) loadTask(id);
});
$('manual-task-id').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('btn-load-task').click();
});
$('btn-refresh').addEventListener('click', () => {
  if (state.taskId) loadTask(state.taskId);
  loadTaskList();
});

/* ---------- 启动 ---------- */
renderAll();
loadTaskList();
if (state.taskId) loadTask(state.taskId);
