/**
 * dsh-sim · panels/reviewer/app.js（WP-15 审查工作台）
 * 定义书 §审查工作台：交互定义 五区域实现；§审查可以接受什么：
 * 存在数值 FAIL / 证据 INSUFFICIENT / 范围 UNCONFIRMED / MOCK / 未关闭阻塞问题时
 * ACCEPT 必须失败 —— 面板层禁用接受按钮并逐条列出原因；首版不提供
 * "带着阻塞强行接受"的按钮。
 *
 * 全程只读证据；审查人身份走 X-Dev-Roles=REVIEWER（开发模式，生产走受信会话）。
 */
import { apiGet, apiPost } from '../shared/api.js';
import {
  el, esc, badge, evidenceModeBadge, digestShort, traceRow,
  blockerList, evidenceChain, offlineBanner, errorBlock, fmtTime,
} from '../shared/components.js';

// 项目不在此硬编码：shared/api.js 默认 dshsim.projects=proj_a（与 MCP/worker 同一
// 专用测试项目），需要时用 localStorage 覆盖，显式配置优先于默认值。
const IDENTITY = { subject: 'dev-reviewer', roles: 'REVIEWER' };

const state = {
  reviewId: new URLSearchParams(location.search).get('review') || null,
  review: null,
  task: null,
  revision: null,
  bundle: null,
  runs: [],
  preparation: null,
  claims: [],
  verifications: [],
  offline: false,
};

const $ = (id) => document.getElementById(id);

async function tryGet(path, label) {
  const r = await apiGet(path, { identity: IDENTITY });
  if (r.offline) { state.offline = true; return null; }
  if (r.error) { console.warn(`[reviewer] ${label} 读取失败`, r.error); return { __error: r.error }; }
  return r.data;
}

const ok = (x) => x && !x.__error;

/* ---------- 顶栏 / 离线 ---------- */
function renderTopbar() {
  const slot = $('evidence-mode-slot');
  slot.replaceChildren();
  const mode = state.offline ? 'UNKNOWN'
    : (state.bundle?.evidence_mode || state.review?.evidence_mode || 'UNKNOWN');
  slot.append(evidenceModeBadge(mode));
  if (mode === 'MOCK') slot.append(el('span', { class: 'mock-watermark', text: 'MOCK' }));
}
function renderOffline() {
  const slot = $('offline-slot');
  slot.replaceChildren();
  if (state.offline) slot.append(offlineBanner('无法连接工程服务。审查动作已禁用，显示内容不代表真实审查状态。'));
}

/* ---------- 阻塞清单计算（区域 2，也驱动接受按钮禁用） ---------- */
function computeBlockers() {
  const out = [];
  const spec = ok(state.revision) ? state.revision.spec : null;

  // 工况缺失
  if (spec && Array.isArray(spec.variants) && Array.isArray(spec.conditions)) {
    const missing = [];
    for (const v of spec.variants) {
      for (const c of spec.conditions) {
        const run = state.runs.find((r) => r.variant_id === v.variant_id && r.condition_id === c.condition_id);
        if (!run) missing.push(`${v.variant_id}×${c.condition_id}`);
      }
    }
    if (missing.length) {
      out.push({ kind: '工况缺失', code: 'MISSING_RUNS', items: missing, hint: '缺少必需工况不能靠减少清单通过；需补算或新建修订缩小范围。' });
    }
  }

  // 数值 FAIL / 证据 INSUFFICIENT / 范围 UNCONFIRMED
  const fails = state.runs.filter((r) => r.numerical_state === 'FAIL').map((r) => `${r.run_id}（${r.variant_id}×${r.condition_id}）`);
  const insuf = state.runs.filter((r) => r.numerical_state === 'INSUFFICIENT').map((r) => r.run_id);
  const unconf = state.runs.filter((r) => r.applicability_state === 'UNCONFIRMED' || r.applicability_state === 'OUT_OF_SCOPE').map((r) => `${r.run_id}（${r.applicability_state}）`);
  if (fails.length) out.push({ kind: '数值 FAIL', code: 'NUMERICAL_FAIL', items: fails, hint: '程序成功不等于数值通过；FAIL 必须退回处理。' });
  if (insuf.length) out.push({ kind: '证据 INSUFFICIENT', code: 'EVIDENCE_INSUFFICIENT', items: insuf, hint: '缺值/来源不明为证据不足；可发起补算或补充证据。' });
  if (unconf.length) out.push({ kind: '范围 UNCONFIRMED', code: 'SCOPE_UNCONFIRMED', items: unconf, hint: '没有方法适用证据不能显示为可用设计结论。' });

  // 输入 STALE（包/审查有效性）
  if (ok(state.bundle) && state.bundle.validity === 'STALE') {
    out.push({ kind: '输入 STALE', code: 'BUNDLE_STALE', items: [`bundle ${state.bundle.bundle_id} 已过期（输入已有新修订）`], hint: '旧包不能继续批准新修订；需基于当前修订重建证据包。' });
  }
  if (ok(state.review) && state.review.validity === 'STALE') {
    out.push({ kind: '输入 STALE', code: 'REVIEW_STALE', items: [`review ${state.review.review_id} 对应修订已过期`], hint: '历史决定保留但不再适用当前输入。' });
  }

  // MOCK 证据
  const mode = state.bundle?.evidence_mode || state.review?.evidence_mode;
  if (mode === 'MOCK') {
    out.push({ kind: 'MOCK 数据', code: 'MOCK_EVIDENCE', items: ['证据包含 MOCK 数据'], hint: 'MOCK 绝不冒充 REAL；含 MOCK 的包不得接受。' });
  }

  // 未关闭问题
  if (ok(state.review) && Array.isArray(state.review.issues)) {
    const open = state.review.issues.filter((i) => i.status !== 'CLOSED');
    if (open.length) {
      out.push({
        kind: '未关闭问题', code: 'OPEN_ISSUES',
        items: open.map((i) => `${i.issue_id}（${i.status} · ${i.severity} · 责任人 ${i.responsible}）`),
        hint: '答复文本本身不能自动关闭阻塞问题；须审查人核对回复与新证据后关闭。',
      });
    }
  }
  return out;
}

function renderBlockers() {
  const body = $('blockers-body');
  body.replaceChildren();
  if (!ok(state.review)) { body.append(el('p', { class: 'muted-line', text: '审查未加载，无法计算阻塞。' })); return; }
  const blockers = computeBlockers();
  if (!blockers.length) {
    body.append(el('p', { class: 'ok-line', text: '未发现阻塞项（仍须以服务端正门校验为准；面板禁用不代替服务端授权）。' }));
    return;
  }
  // 计数汇总
  body.append(el('div', { class: 'blocker-counts' }, blockers.map((b) =>
    el('span', { class: 'badge b-err', text: `${b.kind} × ${b.items.length}`, title: b.code }))));
  // 明细
  for (const b of blockers) {
    body.append(el('div', { class: 'blocker-group' }, [
      el('h3', { text: `${b.kind}（${b.items.length}）` }),
      el('ul', {}, b.items.map((it) => el('li', { text: it }))),
      el('p', { class: 'muted-line', text: `处置：${b.hint}` }),
    ]));
  }
}

/* ---------- 区域 1：审查摘要 ---------- */
function renderSummary() {
  const body = $('summary-body');
  body.replaceChildren();
  const r = state.review;
  if (!r) { body.append(el('p', { class: 'muted-line', text: '尚未加载审查。' })); return; }
  if (r.__error) { body.append(errorBlock(r.__error)); return; }

  const facts = el('dl', { class: 'fact-grid' });
  facts.append(
    el('dt', { text: '审查 ID' }), el('dd', {}, [el('code', { text: r.review_id })]),
    el('dt', { text: '批准用途' }), el('dd', { text: `${r.purpose || state.task?.purpose || 'design_screening'}（接受仅表示同意该冻结包用于本轮批准用途）` }),
    el('dt', { text: '输入修订' }), el('dd', { text: `任务 ${r.task_id} · R${r.revision}` }),
    el('dt', { text: '包摘要' }), el('dd', {}, [digestShort(r.bundle_digest)]),
    el('dt', { text: '审查人' }), el('dd', { text: r.reviewer_id || '—' }),
    el('dt', { text: '状态' }), el('dd', {}, [badge(r.state), document.createTextNode(' '), badge(r.validity)]),
  );
  body.append(facts);

  body.append(el('h3', { text: '未覆盖范围' }));
  body.append(el('p', { text: r.uncovered_scope || state.bundle?.uncovered_scope || '（服务端未单独返回未覆盖范围；以阻塞清单中的工况缺失为准）' }));

  body.append(el('h3', { text: '与上轮意见差异' }));
  body.append(el('p', { class: 'muted-line', text: r.previous_round_diff || '（上轮意见差异接口未就绪；历史决定不可变，可在决定记录中查询）' }));
}

/* ---------- 区域 3：证据抽查链 ---------- */
function renderEvidence() {
  const body = $('evidence-body');
  body.replaceChildren();
  if (!ok(state.bundle)) {
    body.append(el('p', { class: 'muted-line', text: '证据包未就绪（离线或未构建），无法抽查。' }));
    if (state.bundle?.__error) body.append(errorBlock(state.bundle.__error));
    return;
  }

  const claims = state.claims;
  const chainItems = [];
  // 层 1：结论（Claim）
  for (const c of claims.slice(0, 6)) {
    chainItems.push({
      level: '结论', title: `${c.metric_id}（${c.state} · ${c.author_type}）`, summary: c.text,
      artifact_id: c.artifact_id, expandable: true,
      children: [
        { level: '指标定义', title: c.metric_id, summary: '指标口径与公式见能力包 metric-definitions.json（随包冻结）。', digest: ok(state.revision) ? state.revision.spec?.method?.metric_definitions_sha256 : null },
        { level: '原始数值', title: `artifact ${c.artifact_id}`, summary: '原始 CSV / 软件报告，只读保存；禁止从云图估读或模型推测数值。', artifact_id: c.artifact_id },
        { level: '实际设置', title: '真实回读 ReadbackSet', summary: ok(state.preparation) ? `readback 摘要见准备 ${state.preparation.preparation_id}（软件构建 ${state.preparation.software_build || '未知'}）。` : '准备/回读数据未就绪。', digest: ok(state.preparation) ? state.preparation.readback_sha256 : null },
        { level: '方法范围', title: '适用性', summary: '方法适用范围与关键简化依据见能力包 domain.json；范围外为 UNCONFIRMED，不沿用原结论。' },
      ],
    });
  }
  if (!claims.length) {
    chainItems.push({ level: '结论', title: '（无 Claim 数据）', summary: 'claims 接口未就绪或包内无结论；不完整的结论必须标为未完成。' });
  }
  body.append(evidenceChain(chainItems));

  // 独立复算
  body.append(el('h3', { text: '独立复算（Verifier，不照抄报告）' }));
  const btnRow = el('div', { class: 'btn-row' });
  const btnRecheck = el('button', { class: 'btn', type: 'button', text: '对选中 Run 触发独立复算' });
  btnRecheck.disabled = !state.runs.length || state.offline;
  btnRecheck.addEventListener('click', onRecheck);
  btnRow.append(btnRecheck);
  body.append(btnRow);

  if (state.verifications.length) {
    const table = el('table', { class: 'grid' });
    table.append(el('tr', {}, ['verification_id', 'run_id', '结论', '规则集摘要', '时间'].map((h) => el('th', { text: h }))));
    for (const v of state.verifications) {
      table.append(el('tr', {}, [
        el('td', {}, [el('code', { text: v.verification_id })]),
        el('td', { text: v.run_id }),
        el('td', {}, [badge(v.conclusion)]),
        el('td', {}, [digestShort(v.rule_set_sha256)]),
        el('td', { text: fmtTime(v.created_at) }),
      ]));
    }
    body.append(table);
  } else {
    body.append(el('p', { class: 'muted-line', text: '暂无复算记录（或 verifications 接口未就绪，NOT_RUN）。' }));
  }
}

async function onRecheck() {
  const runId = state.runs[0]?.run_id;
  if (!runId) return;
  const confirmed = await showModal('触发独立复算', [
    el('p', { text: `将对 Run ${runId} 由独立 Verifier 从原始数值重新计算派生指标。` }),
    el('p', { class: 'muted-line', text: '复算只证明提取与算术一致性，不证明物理模型已验证。' }),
  ], { confirmText: '开始复算' });
  if (!confirmed) return;
  const r = await apiPost('/verifications/recheck', { run_id: runId }, { identity: IDENTITY, actionPrefix: 'recheck' });
  if (r.error) {
    showModal('复算接口未就绪', [errorBlock(r.error), el('p', { class: 'muted-line', text: '如实降级：本次复算 NOT_RUN。' })]);
    return;
  }
  await loadVerifications();
  renderEvidence();
}

/* ---------- 区域 4：整改区域 ---------- */
function renderIssues() {
  const body = $('issues-body');
  body.replaceChildren();
  if (!ok(state.review)) { body.append(el('p', { class: 'muted-line', text: '审查未加载。' })); return; }
  const issues = state.review.issues || [];
  if (!issues.length) { body.append(el('p', { class: 'ok-line', text: '当前无问题单。' })); }

  for (const issue of issues) {
    body.append(issueCard(issue));
  }

  // 新建问题（审查人起草；DRAFT 需 confirmIssue 人工确认转 OPEN）
  body.append(el('h3', { text: '新建问题（创建后为 DRAFT，需人工确认转 OPEN）' }));
  const form = el('div', { class: 'issue-form' });
  form.append(
    field('责任人 responsible', 'new-responsible'),
    field('严重度 severity（如 BLOCKER/MAJOR/MINOR）', 'new-severity'),
    field('具体问题 description', 'new-description', true),
    field('关闭判据 close_criteria', 'new-close-criteria', true),
  );
  const btnCreate = el('button', { class: 'btn', type: 'button', text: '创建问题（DRAFT）' });
  btnCreate.disabled = state.offline;
  btnCreate.addEventListener('click', onCreateIssue);
  form.append(el('div', { class: 'btn-row' }, [btnCreate]));
  body.append(form);
}

function field(label, id, textarea = false) {
  return el('label', { class: 'field' }, [
    el('span', { text: label }),
    textarea ? el('textarea', { id }) : el('input', { id, type: 'text' }),
  ]);
}

function issueCard(issue) {
  const card = el('div', { class: `issue-card status-${issue.status}` });
  card.append(el('div', { class: 'issue-head' }, [
    el('code', { text: issue.issue_id }),
    badge(issue.status === 'CLOSED' ? 'PASS' : issue.status === 'OPEN' ? 'FAIL' : 'NOT_CHECKED',
      { zhOverride: `问题${issue.status === 'CLOSED' ? '已关闭' : issue.status === 'OPEN' ? '待整改' : '草稿待确认'}` }),
    el('span', { class: 'badge b-warn', text: `severity: ${issue.severity}` }),
    el('span', { class: 'muted-line', text: `责任人：${issue.responsible} · 创建者：${issue.created_by} · v${issue.version}` }),
  ]));
  card.append(el('p', { text: issue.description }));
  card.append(el('p', { class: 'muted-line', text: `关闭判据：${issue.close_criteria}` }));

  // 回复记录
  const replies = issue.replies || [];
  if (replies.length) {
    const ul = el('ul', { class: 'reply-list' });
    for (const rep of replies) {
      ul.append(el('li', {}, [
        el('strong', { text: `${rep.author_id}：` }), document.createTextNode(rep.body),
        el('span', { class: 'muted-line', text: `（${fmtTime(rep.created_at)}${rep.evidence_artifact_ids?.length ? ` · 新证据 ${rep.evidence_artifact_ids.join('、')}` : ''}）` }),
      ]));
    }
    card.append(ul);
  }

  // 回复表单
  const replyBox = el('div', { class: 'reply-form' });
  const ta = el('textarea', { placeholder: '回复内容（答复文本本身不能自动关闭问题）' });
  const ev = el('input', { type: 'text', placeholder: '新证据 artifact_id（逗号分隔，可空）' });
  const btnReply = el('button', { class: 'btn btn-xs', type: 'button', text: '提交回复' });
  btnReply.disabled = state.offline;
  btnReply.addEventListener('click', async () => {
    const ids = ev.value.split(',').map((s) => s.trim()).filter(Boolean);
    const r = await apiPost(`/issues/${issue.issue_id}/replies`, { body: ta.value, evidence_artifact_ids: ids }, { identity: IDENTITY, actionPrefix: 'reply' });
    if (r.error) { showModal('回复失败', [errorBlock(r.error)]); return; }
    await loadReview(state.reviewId);
  });
  replyBox.append(ta, ev, el('div', { class: 'btn-row' }, [btnReply]));
  card.append(replyBox);

  // 动作：确认 DRAFT→OPEN / 关闭
  const actions = el('div', { class: 'btn-row' });
  if (issue.status === 'DRAFT') {
    const btnConfirm = el('button', { class: 'btn btn-xs', type: 'button', text: '确认转 OPEN（一次性人工确认）' });
    btnConfirm.disabled = state.offline;
    btnConfirm.addEventListener('click', () => onConfirmIssue(issue));
    actions.append(btnConfirm);
  }
  if (issue.status === 'OPEN') {
    const hasEvidence = replies.some((r) => (r.evidence_artifact_ids || []).length > 0);
    const btnClose = el('button', { class: 'btn btn-xs', type: 'button', text: '关闭问题（审查人确认）' });
    // 关闭按钮只在审查人身份且证据齐全时可用（开发模式下身份见 X-Dev-Roles=REVIEWER）
    btnClose.disabled = state.offline || !hasEvidence;
    btnClose.title = hasEvidence ? '审查人核对回复与新增证据后关闭' : '尚无带新证据的回复，证据不齐全不可关闭';
    btnClose.addEventListener('click', () => onCloseIssue(issue));
    actions.append(btnClose);
    if (!hasEvidence) actions.append(el('span', { class: 'muted-line', text: '缺新证据，关闭不可用。' }));
  }
  if (issue.status === 'CLOSED') {
    actions.append(el('span', { class: 'muted-line', text: `由 ${issue.closed_by || '—'} 于 ${fmtTime(issue.closed_at)} 关闭` }));
  }
  card.append(actions);
  return card;
}

async function onCreateIssue() {
  const body = {
    responsible: $('new-responsible').value.trim(),
    severity: $('new-severity').value.trim(),
    description: $('new-description').value.trim(),
    close_criteria: $('new-close-criteria').value.trim(),
  };
  if (!body.responsible || !body.severity || !body.description || !body.close_criteria) {
    showModal('字段不全', [el('p', { text: '责任人、严重度、具体问题、关闭判据缺一不可（定义书 §整改项字段）。' })]);
    return;
  }
  const r = await apiPost(`/reviews/${state.reviewId}/issues`, body, { identity: IDENTITY, actionPrefix: 'issue' });
  if (r.error) { showModal('创建失败', [errorBlock(r.error)]); return; }
  await loadReview(state.reviewId);
}

async function onConfirmIssue(issue) {
  const conf = await apiPost('/confirmations', { action: 'confirmIssue', target_id: issue.issue_id, target_digest: `issue-v${issue.version}` }, { identity: IDENTITY, actionPrefix: 'confirm' });
  if (conf.error) { showModal('确认凭据签发失败', [errorBlock(conf.error)]); return; }
  const r = await apiPost(`/issues/${issue.issue_id}/confirm`, { issue_version: issue.version, confirmation_id: conf.data.confirmation_id }, { identity: IDENTITY, actionPrefix: 'confirmissue' });
  if (r.error) { showModal('确认失败', [errorBlock(r.error)]); return; }
  await loadReview(state.reviewId);
}

async function onCloseIssue(issue) {
  const evidenceIds = (issue.replies || []).flatMap((r) => r.evidence_artifact_ids || []);
  const okGo = await showModal('关闭问题（一次性人工确认）', [
    el('p', { text: `关闭 ${issue.issue_id} 前请确认：回复与新增证据满足关闭判据。` }),
    el('p', { class: 'muted-line', text: `关闭判据：${issue.close_criteria}` }),
    el('p', { text: `随附证据：${evidenceIds.join('、') || '（无）'}` }),
  ], { confirmText: '确认关闭' });
  if (!okGo) return;
  const conf = await apiPost('/confirmations', { action: 'closeIssue', target_id: issue.issue_id, target_digest: `issue-v${issue.version}` }, { identity: IDENTITY, actionPrefix: 'confirm' });
  if (conf.error) { showModal('确认凭据签发失败', [errorBlock(conf.error)]); return; }
  const r = await apiPost(`/issues/${issue.issue_id}/close`, {
    issue_version: issue.version,
    close_evidence_artifact_ids: evidenceIds,
    confirmation_id: conf.data.confirmation_id,
  }, { identity: IDENTITY, actionPrefix: 'closeissue' });
  if (r.error) { showModal('关闭失败', [errorBlock(r.error)]); return; }
  await loadReview(state.reviewId);
}

/* ---------- 区域 5：决定区域 ---------- */
function renderDecision() {
  const body = $('decision-body');
  body.replaceChildren();
  if (!ok(state.review)) { body.append(el('p', { class: 'muted-line', text: '审查未加载。' })); return; }
  const r = state.review;
  const blockers = computeBlockers();
  const decidable = ['PENDING', 'CHANGES_REQUESTED'].includes(r.state) && !state.offline;

  const btnRow = el('div', { class: 'btn-row' });
  const btnReturn = el('button', { class: 'btn', type: 'button', text: '退回补充' });
  btnReturn.disabled = !decidable;
  btnReturn.addEventListener('click', () => onDecide('REQUEST_CHANGES'));
  const btnAccept = el('button', { class: 'btn btn-primary', type: 'button', text: '接受本轮' });
  btnAccept.disabled = !decidable || blockers.length > 0;
  btnAccept.addEventListener('click', () => onAccept());
  // 拒绝放次级菜单
  const btnReject = el('button', { class: 'btn btn-danger btn-xs', type: 'button', text: '拒绝（次级动作）' });
  btnReject.disabled = !decidable;
  btnReject.addEventListener('click', () => onDecide('REJECT'));
  btnRow.append(btnReturn, btnAccept, el('span', { class: 'secondary-action' }, [btnReject]));
  body.append(btnRow);

  if (blockers.length) {
    body.append(el('div', { class: 'accept-disabled' }, [
      el('p', { class: 'warn-line', text: '接受按钮已禁用，原因逐条如下（没有"带着阻塞强行接受"的按钮）：' }),
      el('ul', {}, blockers.map((b) => el('li', { text: `${b.kind} × ${b.items.length}：${b.items[0]}${b.items.length > 1 ? ' 等' : ''}` }))),
    ]));
  }
  if (state.offline) body.append(el('p', { class: 'warn-line', text: '服务离线，所有审查动作已禁用。' }));
  body.append(el('p', { class: 'muted-line', text: '决定绑定人、包摘要、任务修订、用途和时间；已接受决定不可变。面板禁用不代替服务端授权（服务端在 ACCEPT 事务内再次校验）。' }));
}

async function onAccept() {
  const r = state.review;
  const okGo = await showModal('接受本轮（一次性确认）', [
    el('p', { text: '接受仅表示指定业务角色同意该冻结包用于本轮批准用途。' }),
    el('dl', { class: 'fact-grid' }, [
      el('dt', { text: '用途' }), el('dd', { text: r.purpose || 'design_screening（内部方案筛选）' }),
      el('dt', { text: '任务/修订' }), el('dd', { text: `${r.task_id} · R${r.revision}` }),
      el('dt', { text: '包摘要' }), el('dd', {}, [digestShort(r.bundle_digest)]),
    ]),
    el('label', { class: 'field' }, [el('span', { text: '使用限制 limitations（将写入决定，可空）' }), el('textarea', { id: 'accept-limitations' })]),
    el('p', { class: 'warn-line', text: '确认后绑定当前摘要与修订；新输入将使其对当前输入失效而非删除历史。' }),
  ], { confirmText: '一次性确认并接受' });
  if (!okGo) return;
  await onDecide('ACCEPT', document.getElementById('accept-limitations')?.value || null);
}

async function onDecide(outcome, limitations = null) {
  const r = state.review;
  const conf = await apiPost('/confirmations', { action: 'decideReview', target_id: r.review_id, target_digest: r.bundle_digest }, { identity: IDENTITY, actionPrefix: 'confirm' });
  if (conf.error) { showModal('确认凭据签发失败', [errorBlock(conf.error)]); return; }
  const resp = await apiPost(`/reviews/${r.review_id}/decisions`, {
    outcome,
    bundle_digest: r.bundle_digest,
    revision: r.revision,
    limitations,
    confirmation_id: conf.data.confirmation_id,
  }, { identity: IDENTITY, actionPrefix: 'decide' });
  if (resp.error) { showModal('决定失败（服务端正门拒绝）', [errorBlock(resp.error)]); return; }
  showModal('决定已记录', [
    el('p', { text: `outcome=${resp.data.outcome} · decided_by=${resp.data.decided_by}` }),
    el('p', { class: 'muted-line', text: '决定不可变；新修订使旧决定仅历史可见。' }),
  ]);
  await loadReview(state.reviewId);
}

/* ---------- 模态 ---------- */
function showModal(title, bodyNodes, { confirmText = null, danger = false } = {}) {
  return new Promise((resolve) => {
    const root = $('modal-root');
    root.replaceChildren();
    const close = (val) => { root.replaceChildren(); resolve(val); };
    const btns = el('div', { class: 'btn-row' });
    if (confirmText) {
      const okBtn = el('button', { class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`, type: 'button', text: confirmText });
      okBtn.addEventListener('click', () => close(true));
      const no = el('button', { class: 'btn', type: 'button', text: '取消' });
      no.addEventListener('click', () => close(false));
      btns.append(no, okBtn);
    } else {
      const okBtn = el('button', { class: 'btn', type: 'button', text: '关闭' });
      okBtn.addEventListener('click', () => close(true));
      btns.append(okBtn);
    }
    const modal = el('div', { class: 'modal', role: 'dialog', 'aria-label': title }, [el('h2', { text: title }), ...[].concat(bodyNodes), btns]);
    const mask = el('div', { class: 'modal-mask' }, [modal]);
    mask.addEventListener('click', (e) => { if (e.target === mask) close(false); });
    root.append(mask);
  });
}

/* ---------- 数据加载 ---------- */
async function loadVerifications() {
  const data = await tryGet(`/verifications?task_id=${state.review.task_id}`, '复算记录');
  if (data && !data.__error) state.verifications = Array.isArray(data.items) ? data.items : [];
}

async function loadReview(reviewId) {
  state.reviewId = reviewId;
  state.review = state.task = state.revision = state.bundle = state.preparation = null;
  state.runs = []; state.claims = []; state.verifications = [];
  state.offline = false;
  renderAll();

  state.review = await tryGet(`/reviews/${reviewId}`, '审查');
  if (!ok(state.review)) { renderAll(); return; }
  const taskId = state.review.task_id;

  const [task, revision, bundle, prep, runs] = await Promise.all([
    tryGet(`/tasks/${taskId}`, '任务'),
    tryGet(`/tasks/${taskId}/revisions/latest`, '修订'),
    tryGet(`/bundles/${state.review.bundle_id}`, '证据包'),
    tryGet(`/tasks/${taskId}/preparations/latest`, '准备'),
    tryGet(`/tasks/${taskId}/runs`, 'Run 列表'),
  ]);
  state.task = task; state.revision = revision; state.bundle = bundle; state.preparation = prep;
  if (runs && !runs.__error) state.runs = Array.isArray(runs.items) ? runs.items : [];

  if (ok(bundle)) {
    const claims = await tryGet(`/bundles/${bundle.bundle_id}/claims`, 'Claim');
    if (claims && !claims.__error) state.claims = Array.isArray(claims.items) ? claims.items : [];
  }
  await loadVerifications();
  renderAll();
}

function renderAll() {
  renderTopbar();
  renderOffline();
  renderSummary();
  renderBlockers();
  renderEvidence();
  renderIssues();
  renderDecision();
}

/* ---------- 启动 ---------- */
$('btn-load-review').addEventListener('click', () => {
  const id = $('manual-review-id').value.trim();
  if (id) loadReview(id);
});
$('manual-review-id').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-load-review').click(); });
$('btn-refresh').addEventListener('click', () => { if (state.reviewId) loadReview(state.reviewId); });

renderAll();
if (state.reviewId) loadReview(state.reviewId);
