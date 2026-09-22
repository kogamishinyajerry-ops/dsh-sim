# dsh-sim 插件 UI 设计书（DSH 0.1.5-rc.2 发布包适配版）

> 版本 v1.0 / 2026-09-21。本文回答一个问题：**工业仿真能力装进 DSH 后，UI 应该怎么长，才符合工程师与审查人的心智模型**。
> 依据：DSH-SIM-DDD-001 定义书（§执行工作台/§审查工作台/§三张发布底线）+ dsh-visual-plugin 0.3.2 实证的 0.1.5 插件机制。

---

## 1. 用户心智模型分析（设计的出发点）

| 角色 | 心智模型（他们怎么想的） | UI 必须对应成 |
|---|---|---|
| 执行工程师 | "我接了活 → 它要我先确认 → 我盯着它算 → 出问题叫我" | **任务队列按"需要我处理"排序置顶**；确认是显式按钮+对照表，不是聊天里说"我同意" |
| 审查人 | "什么阻止我签字？这个数从哪来的？" | **阻塞清单永远在最上**；每个数字可点穿到原始 artifact；没有"带阻塞强行通过"的按钮 |
| 团队同事 | "那个仿真现在跑到哪了？我要不要介入？" | **打开任务即见服务器当前真态**（不是历史聊天记录）：阶段+心跳+残差+云图，失联即条纹告警 |
| 智能体用户 | "帮我干活，但别越权" | Agent 工具结果**先给结论+状态徽章+下一动作提示**；授权/接受永远返回"这需要人工在面板完成" |

**三条不可妥协的信任直觉**（定义书底线翻译成 UI 语言）：
1. **真假分明**：REAL/MOCK/UNKNOWN 三态徽章常驻每屏顶栏；MOCK 永远琥珀+斜纹，绝不染绿。
2. **数必有源**：每个数字旁边就有来源短哈希，点击直达原始文件；查不到来源的数字不允许出现。
3. **未知即醒目**：失联/LOST/证据缺失用条纹底+文字，不用灰淡处理（那会让人以为没事）。

## 2. 信息架构：两个平面 + 一个会话流

```
┌─ DSH 会话流（对话）───────────────────────────────┐
│  智能体调 12 工具 → 消息卡输出结构化状态+徽章+提示   │
│  深度动作（授权/接受）→ 消息卡只给"请到工作台完成"   │
├─ 右侧栏「仿真工作台」面板（常驻、可切换任务）────────┤
│  执行台视图 / 审查台视图 / 监控实况                 │
│  → 内嵌 /panels 页（同一套 UI，iframe 加载自工程API）│
├─ 设置页「仿真服务」卡片（管理员/首次配置）───────────┤
│  API 地址、身份、连通性自检、能力包清单、版本基线     │
└──────────────────────────────────────────────────┘
```

**为什么是"右侧面板 + iframe"，而不是把面板重写成原生槽位组件？**
- 0.1.5 的 `main`/`conversation.*` 槽位官方组件（ConversationPanel）占据主区，第三方抢主区等于和对话流对抗——违背心智模型。
- 官方先例（dsh-visual-plugin 0.3.2、sidebar-files）证明 `sidebar.right.pane.tab` 是第三方标准位：**不打扰对话、随手可开、可常驻**。
- iframe 让面板代码（已有完整交互定义）**一处维护、两处可用**（DSH 内 + 纯浏览器应急通道）；这也满足定义书"受保护审查页作为同一任务入口的临时实现"的降级条款，只是我们把"临时"做成了"首选"。
- React 18 + `window.__ModuleLoader__` 机制已实证（host 提供 `require("react")`），插件 client.js 无打包依赖、纯手写即可，**离线可分发的硬前提**。

## 3. 插件实体设计（package：`dsh-sim-plugin`）

### 3.1 结构（离线自包含，无 npm 依赖）

```
dsh-sim-plugin/
├── package.json          # dsh.bundle.patch + dsh.client(platform:web, inject 列表)
├── cordis.patch.yml      # bundle 层：注册 sim-bridge loader entry
├── lib/
│   ├── index.js          # 服务端：settings 注册 + /dsh-sim/* HTTP 前缀代理（webServer.register）
│   └── client.js         # 客户端：locale + 三个槽位注册（React 18 jsx-runtime，宿主提供）
└── README.md             # 内网导入指引
```

### 3.2 服务端 `lib/index.js`（少即是多）

| 职责 | 实现 | 为什么 |
|---|---|---|
| 设置命名空间 `dsh-sim` | `settings.register(NS, schema, {base})`：apiUrl / identity subject / roles / pollIntervalMs | 同事各自配置自己的工程 API 端点与身份；改动即存 DSH settings |
| HTTP 前缀代理 `/dsh-sim/*` → 工程 API | `webServer.register({kind:"prefix", handler})`，注入身份头（dev 模式） | 面板 iframe 与 DSH 会话**同源同端口**访问工程 API：无 CORS、无第二个端口要开防火墙、凭据不落消息文本 |
| 健康探针 `/dsh-sim/health` | 代理 + 本地回环检查 | 设置卡片与工作台顶栏的"工程服务可用性"徽章数据源 |

**明确不做**：不在服务端复制工程 API 逻辑（真相在 8600 服务）；不缓存任务状态；不写文件（除 DSH_HOME 下插件数据目录的只读缓存）。

### 3.3 客户端 `lib/client.js`（三个槽位 + locale）

```js
window.__ModuleLoader__.load({
  id: "dsh-sim-plugin",
  factory: (require) => {
    const react = require("react");
    const jsx = require("react/jsx-runtime");
    // apply(ctx):
    // 1) ctx.locale.register("dsh-sim", {zh, en})
    // 2) ctx.slots.inject("sidebar.right.pane.tab",   → SimWorkbenchBody)
    //    ctx.slots.inject("sidebar.right.pane.tab.title", → SimWorkbenchTitle)
    // 3) ctx.slots.inject("settings.plugin.item",     → SimSettingsCard)
  }
})
```

- **SimWorkbenchBody**：头部三枚视图切换（执行台/审查台/监控）+ 同源 iframe（`/dsh-sim/panels/executor/index.html` 等，经代理前缀）+ 顶栏徽章（服务可用性 REAL/UNAVAILABLE、身份、轮询间隔）。
- **SimWorkbenchTitle**：图标 + "仿真工作台" + 运行中任务计数（从 settings/健康数据拉）。
- **SimSettingsCard**：apiUrl 输入、身份（subject/roles 下拉）、轮询间隔、"测试连接"按钮（调 `/dsh-sim/health` 显示 ✅/❌ + trace_id）、当前能力包清单（RELEASED/DRAFT 徽章）、版本基线行（dsh 0.1.5-rc.2 / 插件版本 / 适配器构建）。

### 3.4 会话流内的工具结果卡（不改 MCP 工具，改"输出形状"）

12 个工具返回值统一附加 `_ui_hint` 字段（结构化、模型与人类双读）：

```json
{
  "...": "原数据",
  "_ui_hint": {
    "state_badge": "READY|BLOCKED|...",
    "headline": "一句话结论",
    "next_actions": ["到右侧「仿真工作台」完成人工授权", "get_run(run_id) 轮询"],
    "evidence_mode": "REAL|MOCK|UNKNOWN",
    "deep_link": "/dsh-sim/panels/executor/index.html?task=task_xxx"
  }
}
```

**原则**：模型输出给用户的每句话都能落在这个结构上——状态有徽章、结论一句话、下一步明确、真假可辨、深链直达。授权/接受类动作**永不提供工具**，只给深链（职责分离在 UI 层的投影）。

## 4. 交互流程（关键路径逐屏）

1. **创建任务**：会话里"帮我做一个 NACA2412 三工况对比"→ 智能体 create_task → 消息卡：DRAFT 徽章 + 缺项清单（field/责任人）+ "去工作台补全"。
2. **确认准备**：工作台执行台视图 → 矩阵 + 申请值/回读值对照 + 差异未清禁用运行 + 主按钮"生成人工确认凭据"→ 一次性确认模态（摘要+用途+过期倒计时）。
3. **监控实况**：提交后执行台运行区 → 阶段徽章（QUEUED→LEASED→RUNNING…）+ 最后心跳计时 + 失联条纹告警；残差迷你曲线（事件流驱动）；云图网格懒加载。
4. **审查介入**：审查台视图 → 阻塞清单置顶（六类计数）→ 证据抽查链逐层展开 → 问题单（起草/确认/关闭三段态）→ 决定区双主按钮，**ACCEPT 阻塞时禁用并列因**。
5. **断线重入**：同事第二天打开 DSH → 右侧面板自动恢复任务当前真态（服务器态为准）→ 过期摘要标 STALE，新旧并列展示。

## 5. 视觉规范（沿用 DSH 令牌，补齐三态）

- 令牌：`--dsw-alias-*`（与视觉插件一致），浅色优先。
- 三态徽章：`REAL`(绿) / `MOCK`(琥珀+斜纹) / `UNKNOWN`(条纹底+"状态未知"文字)。
- 状态徽章**英文枚举原文 + 中文注释**（`RUNNING 运行中`）——调试与文档可对齐，用户不失读。
- 不以颜色为唯一通道：所有告警带文字；表格可复制；打印保留 ID/摘要/trace_id。
- 动效 `prefers-reduced-motion` 全局降级。

## 6. 与"符合人类心智模型"的核对清单（设计自检）

- [x] 工程师打开插件第一眼看到的是"需要我处理什么"而非功能列表
- [x] 任何"工程通过"字样不存在（只有多维状态分列）
- [x] Agent 无法从 UI 层获得授权/接受能力（深链导向人工面板，凭据一次性）
- [x] 每个数字可点穿到来源；查无来源的渲染路径不存在
- [x] 断线重入恢复的是服务器真态，不是聊天记忆
- [x] 离线/服务不可用时显示"工程服务不可用"而非静默空白或编造数据
- [x] 内网同事拿到的是**一个 zip**：解压 → install.ps1 → dsh web 重启 → 三处 UI 出现

## 7. 明确不做（防范围蔓延）

- 不内嵌 3D 查看器（STL/场数据）：v2 用 `export-geom`+three.js 独立评估，本版给 artifact 下载+PNG 云图。
- 不做工具结果自定义 React 渲染器（`tool.call.toolview` 槽）：本轮用 `_ui_hint` 结构化文本达成 90% 效果，渲染器槽作为 v2 候选项（需官方槽文档）。
- 不动 12 个 MCP 工具的定义书边界（不加 approve/run_any_code）。
- 不替换 DSH 会话主区（`main`/`conversation.*`）：那是用户与智能体的对话空间。
