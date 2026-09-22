"""生成单文件 HTML 验证报告（内嵌数据 + Chart.js CDN）。

用法: default-env python scripts/gen_e2e_report.py
"""
from __future__ import annotations

import json

EVID = r"<REPO_ROOT>\var\demo_evidence"
OUT = r"<REPO_ROOT>\docs\e2e-validation-report.html"

TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>dsh-sim 端到端综合仿真验证报告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#0f1115;--sub:#62666b;--line:#e4e6ea;--blue:#4176e6;--green:#1a7f4b;--amber:#a66b00;--red:#c53d3d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 24px 80px}
h1{font-size:26px;letter-spacing:-.02em;margin:0 0 6px}
h2{font-size:19px;margin:44px 0 12px;padding-top:18px;border-top:1px solid var(--line)}
.sub{color:var(--sub);font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:600;vertical-align:middle}
.b-real{background:#e4f4ea;color:var(--green)}
.b-gate{background:#fdeeee;color:var(--red)}
.b-mock{background:#fdf3e0;color:var(--amber)}
.b-info{background:#e7eefc;color:var(--blue)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin:14px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--sub);font-weight:600;font-size:12px}
code,.mono{font-family:Consolas,Menlo,monospace;font-size:12px}
.big{font-size:30px;font-weight:700;letter-spacing:-.02em}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
@media(max-width:760px){.kpi{grid-template-columns:repeat(2,1fr)}}
.kpi .card{margin:0;text-align:center;padding:16px 10px}
.kv{color:var(--sub);font-size:12px;margin-top:2px}
.flow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0}
.flow .st{padding:4px 10px;border-radius:8px;background:#eef1f6;font-size:12px;font-weight:600}
.flow .ok{background:#e4f4ea;color:var(--green)}
.flow .no{background:#fdeeee;color:var(--red)}
.arrow{color:var(--sub)}
.warn{background:#fdf6e7;border:1px solid #f0dfb2;border-radius:12px;padding:14px 18px;margin:14px 0;font-size:13px}
canvas{max-height:260px}
.footer{margin-top:48px;color:var(--sub);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
</style>
</head>
<body><div class="wrap">
<h1>dsh-sim 端到端综合仿真验证报告</h1>
<div class="sub">三个飞机设计相关任务 · 真实 STAR-CCM+ 2402 (19.02.009-R8) 求解 · 全链路 API 留痕 · 2026-09-21</div>

<div class="kpi">
  <div class="card"><div class="big">3</div><div class="kv">端到端任务（T1/T2/T3）</div></div>
  <div class="card"><div class="big">11</div><div class="kv">闭环阶段全部打通</div></div>
  <div class="card"><span class="badge b-real">REAL</span><div class="kv">T1/T2 真实求解器续算</div></div>
  <div class="card"><span class="badge b-gate">ACCEPT 全部拒绝</span><div class="kv">诚实门精准触发（设计使然）</div></div>
</div>

<div class="warn"><b>阅读须知：</b>本报告中"真实"指 STAR-CCM+ 商业求解器实际执行（ccmpsuite_solve 已验证可用）。<b>未声称工程通过</b>：能力包 buffer_chamber 0.1.0 为 DRAFT（阈值 TBD-05/06 未冻结），按定义书发布门，三任务的 ACCEPT 均被服务端正确拒绝——这本身是本次验证最重要的功能证明。</div>

<h2>① 任务全景</h2>
<div class="card">
<table>
<tr><th>任务</th><th>主题</th><th>文献锚点</th><th>执行模式</th><th>关键真实数据</th><th>终态</th></tr>
<tr><td><b>T1</b></td><td>NACA 2412 翼型气动收敛</td><td class="mono">Abbott &amp; von Doenhoff (1959); NACA R-460</td><td><span class="badge b-real">REAL</span></td><td>510 迭代真实残差序列，收敛至 <span class="mono">1.8e-22</span></td><td>REQUEST_CHANGES</td></tr>
<tr><td><b>T2</b></td><td>圆柱涡街非定常特性</td><td class="mono">Williamson (1996) ARFM 28:477; Roshko (1954)</td><td><span class="badge b-real">REAL</span></td><td>5150 迭代 7 分量残差 + 5000 点监视器导出</td><td>REQUEST_CHANGES</td></tr>
<tr><td><b>T3</b></td><td>A/B 对比可比性门</td><td class="mono">NASA CFD V&amp;V（方法论）</td><td><span class="badge b-info">真实身份 diff</span></td><td>A/B 物理方法差异真实检出</td><td>REQUEST_CHANGES</td></tr>
</table>
</div>

<h2>② 全链路闭环（每个任务完整走通）</h2>
<div class="card">
<div class="flow">
<span class="st ok">create_task</span><span class="arrow">→</span>
<span class="st ok">revise_task</span><span class="arrow">→</span>
<span class="st ok">prepare_task</span><span class="arrow">→</span>
<span class="st ok">Worker PREPARE</span><span class="arrow">→</span>
<span class="st ok">受信确认凭据</span><span class="arrow">→</span>
<span class="st ok">authorizeRuns</span><span class="arrow">→</span>
<span class="st ok">submitRuns</span><span class="arrow">→</span>
<span class="st ok">buildBundle</span><span class="arrow">→</span>
<span class="st ok">submitReview</span><span class="arrow">→</span>
<span class="st no">ACCEPT 被拒</span><span class="arrow">→</span>
<span class="st ok">REQUEST_CHANGES</span>
</div>
<div class="sub" style="margin-top:8px">11 个 API 阶段 × 3 任务全部真实 HTTP 往返，留痕于 <span class="mono">var/demo_evidence/mcp_call_log.json</span>（60+ 条事件记录）</div>
</div>

<h2>③ 安全与职责分离实战验证</h2>
<div class="card">
<table>
<tr><th>攻击/误用场景</th><th>系统响应</th><th>结果</th></tr>
<tr><td>AGENT 身份直接调 authorizeRuns（3 任务各 1 次）</td><td class="mono">403 FORBIDDEN</td><td><span class="badge b-gate">全部拒绝</span></td></tr>
<tr><td>伪造 confirmation_id 授权</td><td class="mono">403 人工确认凭据无效/已消费/已过期/绑定不匹配</td><td><span class="badge b-gate">拒绝</span></td></tr>
<tr><td>准备未完成时抢跑授权</td><td class="mono">503 BLOCKED 准备未完成或存在未清除差异</td><td><span class="badge b-gate">拒绝</span></td></tr>
<tr><td>存在未关闭问题时 ACCEPT</td><td class="mono">503 BLOCKED ACCEPT 必须失败（列出 open_issue_ids）</td><td><span class="badge b-gate">3/3 拒绝</span></td></tr>
<tr><td>AGENT 创建审查问题</td><td>允许但强制 DRAFT，REVIEWER 确认后才 OPEN</td><td><span class="badge b-info">职责分离生效</span></td></tr>
</table>
</div>

<h2>④ T1 · NACA 2412 翼型 — 真实残差收敛</h2>
<div class="grid">
<div class="card"><b>收敛曲线（对数轴）</b><canvas id="c1"></canvas>
<div class="sub">Simcenter STAR-CCM+ 2402 真实续算：500→510 iter，四方程残差全部收敛至 1e-22 量级（Steady · Segregated Flow · Laminar）</div></div>
<div class="card">
<b>文献锚点与限制</b>
<table>
<tr><th>项</th><th>值</th></tr>
<tr><td>物理模型（真实回读）</td><td>Steady · Segregated Flow · Laminar · Constant Density</td></tr>
<tr><td>文献参考</td><td class="mono">Abbott &amp; von Doenhoff, Theory of Wing Sections (1959)</td></tr>
<tr><td>Cl/Cd 定量对齐</td><td><span class="badge b-mock">UNCONFIRMED</span> 参考压力/边界映射未冻结（TBD-05/06）——<b>不声称</b>与文献数值一致</td></tr>
</table>
</div>
</div>

<h2>⑤ T2 · 圆柱涡街（Williamson 锚点）— 真实非定常数据</h2>
<div class="grid">
<div class="card"><b>七分量残差（对数轴）</b><canvas id="c2"></canvas>
<div class="sub">Implicit Unsteady · SST k-omega · 真实续算 30 步（5150 总迭代）激活报告监视器</div></div>
<div class="card">
<b>监视器与限制</b>
<table>
<tr><th>项</th><th>值</th></tr>
<tr><td>导出监视器</td><td>Fx/Fy/SP + 残差 + Energy/Tke/Sdr（5000 点真实序列）</td></tr>
<tr><td>文献参考</td><td class="mono">Williamson (1996): St≈0.19–0.21 (subcritical Re)</td></tr>
<tr><td>St 数值对比</td><td><span class="badge b-mock">UNCONFIRMED</span> 物理时间步 dt 未随模板登记——迭代域谱峰不能直接换算 St，<b>不编造数值</b></td></tr>
</table>
</div>
</div>

<h2>⑥ T3 · A/B 对比可比性门 — 拒绝错误的"改进幅度"</h2>
<div class="card">
<table>
<tr><th>维度</th><th>A（NACA2412）</th><th>B（cyl vortex）</th><th>一致性</th></tr>
<tr><td>时间制式</td><td>Steady</td><td>Implicit Unsteady</td><td><span class="badge b-gate">不一致</span></td></tr>
<tr><td>湍流模型</td><td>Laminar</td><td>SST k-omega</td><td><span class="badge b-gate">不一致</span></td></tr>
<tr><td>流动模型</td><td>Segregated Flow</td><td>Segregated Flow</td><td><span class="badge b-real">一致</span></td></tr>
</table>
<div class="sub" style="margin-top:6px">定义书 §审查动作："定义不一致则禁止给出改进幅度"。dsh_sim 可比性门按此拒绝数值对比，仅允许身份追溯（<span class="mono">t3_ab_identity_diff.json</span> 为真实 sim-summary diff 证据）。<b>这正是防住"拿苹果和橘子比出提升百分比"这类事故的机制证明。</b></div>
</div>

<h2>⑦ 证据链抽查（审查人视角）</h2>
<div class="card">
<table>
<tr><th>任务</th><th>prepared_digest</th><th>bundle_digest</th><th>review</th><th>issue</th><th>决定</th></tr>
<tr><td>T1</td><td class="mono" id="d-t1p"></td><td class="mono" id="d-t1b"></td><td class="mono" id="d-t1r"></td><td class="mono" id="d-t1i"></td><td><span class="badge b-info">REQUEST_CHANGES</span></td></tr>
<tr><td>T2</td><td class="mono" id="d-t2p"></td><td class="mono" id="d-t2b"></td><td class="mono" id="d-t2r"></td><td class="mono" id="d-t2i"></td><td><span class="badge b-info">REQUEST_CHANGES</span></td></tr>
<tr><td>T3</td><td class="mono" id="d-t3p"></td><td class="mono" id="d-t3b"></td><td class="mono" id="d-t3r"></td><td class="mono" id="d-t3i"></td><td><span class="badge b-info">REQUEST_CHANGES</span></td></tr>
</table>
<div class="sub">每个摘要均可从原始 artifact 独立复算（canonical-json-v1 + SHA-256）；审查人无需向执行人另索文件（FR-20 达成路径演示）。</div>
</div>

<h2>⑧ 本次验证证明了什么 / 没证明什么</h2>
<div class="grid">
<div class="card" style="border-top:3px solid var(--green)"><b>已证明（有真实证据）</b>
<table>
<tr><td>✓</td><td>STAR-CCM+ 2402 真实续算链路（license ccmpsuite_solve 可用）</td></tr>
<tr><td>✓</td><td>11 阶段闭环 API 全部真实往返 + 幂等 + 留痕</td></tr>
<tr><td>✓</td><td>AGENT 永远拿不到授权/接受权（多次攻击全部被拒）</td></tr>
<tr><td>✓</td><td>ACCEPT 门在有未关闭问题/UNCONFIRMED 时必败</td></tr>
<tr><td>✓</td><td>A/B 可比性检查拒绝口径不一致的数值对比</td></tr>
<tr><td>✓</td><td>真实监视器/残差序列导出（5000 点级）</td></tr>
</table></div>
<div class="card" style="border-top:3px solid var(--amber)"><b>未证明（诚实边界）</b>
<table>
<tr><td>—</td><td>Cl/Cd 与 Abbott 表定量对齐（需 Owner 冻结参考压力）</td></tr>
<tr><td>—</td><td>Strouhal 数与 Williamson 范围对比（需物理 dt 登记）</td></tr>
<tr><td>—</td><td>工程方法正确性（本版无 RELEASED 能力包）</td></tr>
<tr><td>—</td><td>多人权限真实隔离（开发身份头模式）</td></tr>
<tr><td>—</td><td>新几何自动建模（R0 明确排除）</td></tr>
</table></div>
</div>

<div class="footer">
数据来源：dsh-sim/var/demo_evidence/（真实 STAR-CCM+ 输出 + API 调用日志 + 哈希链）·
求解器：Simcenter STAR-CCM+ 2402 Build 19.02.009-R8 ·
工程服务：dsh-sim 0.1.0 @ 127.0.0.1:8600 ·
报告生成：2026-09-21
</div>
</div>

<script>
const D = __DATA_PLACEHOLDER__;
for (const t of ['t1','t2','t3']) {
  const c = D.chain[t];
  document.getElementById('d-'+t+'p').textContent = c.prepared_digest+'…';
  document.getElementById('d-'+t+'b').textContent = c.bundle_digest+'…';
  document.getElementById('d-'+t+'r').textContent = c.review_id ? c.review_id.slice(0,16)+'…' : '-';
  document.getElementById('d-'+t+'i').textContent = c.issue_id ? c.issue_id.slice(0,16)+'…' : '-';
}
const COLORS = {Continuity:'#4176e6','X-momentum':'#1a7f4b','Y-momentum':'#a66b00','Z-momentum':'#c53d3d',Energy:'#7a5fd0',Tke:'#0f8b8b',Sdr:'#8b6f0f'};
function lineChart(id, series){
  const ctx = document.getElementById(id);
  if(!ctx) return;
  const ds = Object.entries(series).map(([k,v])=>({
    label:k, data:v.map(x=>Math.log10(Math.max(x,1e-30))), borderColor:COLORS[k]||'#888',
    backgroundColor:'transparent', borderWidth:1.6, pointRadius:0, tension:.25
  }));
  new Chart(ctx,{type:'line',data:{labels:series[Object.keys(series)[0]].map((_,i)=>i),datasets:ds},
    options:{animation:false,plugins:{legend:{labels:{boxWidth:10,font:{size:10}}}},
      scales:{y:{title:{display:true,text:'log10(residual)'},ticks:{font:{size:9}}},
              x:{title:{display:true,text:'迭代（降采样序号）'},ticks:{font:{size:9}}}}}});
}
lineChart('c1', D.t1.residuals);
lineChart('c2', D.t2.residuals);
</script>
</body></html>"""


def main() -> None:
    with open(EVID + r"\report_data.json", encoding="utf-8") as f:
        data = f.read()
    html = TEMPLATE.replace("__DATA_PLACEHOLDER__", data)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("report written:", OUT, len(html), "bytes")


if __name__ == "__main__":
    main()
