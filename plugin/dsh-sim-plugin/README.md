# dsh-sim-plugin — 内网导入指南

> 目标：把本插件导入内网 DSH 0.1.5-rc.2（web profile）后，内网同事即可获得完整工业仿真能力。
> 本包**零 npm 依赖**（React/运行时由 DSH 宿主提供），离线可分发。

## 内容物
```
dsh-sim-plugin/
├── package.json          # dsh.bundle + dsh.client 声明（exports 含 ./client）
├── cordis.patch.yml      # bundle patch（loader entry，inject: [settings, webServer]）
├── lib/index.js          # 服务端：settings 命名空间 + /dsh-sim/* 前缀代理 + /dsh-sim/health
├── lib/client.js         # 客户端：右侧「仿真工作台」+ 设置卡片（React 18，宿主提供运行时）
├── install.ps1           # 幂等内网安装脚本（复制→注册→依赖 junction→自检）
└── README.md
```

## 一键安装（内网 DSH 机器上）
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
# 自定义：powershell -ExecutionPolicy Bypass -File install.ps1 -DshHome <DSH_HOME>\home -Profile web
```
脚本做的事（幂等，pnpm 清掉 junction 后可重跑）：
1. 复制插件到 `<DSH_HOME>\home\plugins\dsh-sim-plugin`（稳定位置，pnpm 不会清）
2. `dsh plugin --profile web add <dir> --store-dir=...` 官方注册（link: 依赖 + bundles 条目）
3. 建 4 条依赖 junction（schemastery/dsh-settings/dsh-host-webserver/cordis）——**没有它们 loader 会因 realpath 解析找不到 @deepseek-ai/* 而报 Cannot find package**
4. `--dump-config` 自检 sim-bridge 可解析

## 前置条件
1. DSH 0.1.5-rc.2 已装且 `dsh web` 可启动。
2. dsh-sim 工程服务可达（默认 `http://127.0.0.1:8600/api/v1`，安装后在设置卡片改内网地址）。

## 导入后验证
1. 重启 `dsh web`，启动日志无 sim-bridge 错误。
2. 右侧栏出现「仿真工作台」页签；设置页出现「仿真服务」卡片。
3. 卡片填身份（subject/roles/项目）→ 保存（身份同步给面板 iframe）→ 测试连接：✅ + 能力包清单（buffer_chamber 当前 DRAFT——阈值待 Owner 冻结，是设计行为不是故障）。
4. 工作台三个视图：执行台（需要我处理什么）、审查台（什么阻止接受）、监控。

## 代理路径（同源免 CORS）
- `/dsh-sim/api/*` → 工程 API（面板 fetch 走这里，代理统一注入/透传 X-Dev-* 身份头）
- `/dsh-sim/panels/*` → 面板静态页
- `/dsh-sim/health` → 插件健康探针（含上游连通性）

## 给同事的一句话
打开 DSH → 右侧「仿真工作台」→ 执行台看“需要我处理什么”、审查台看“什么阻止接受”、云图与残差直接可见；断线重开恢复的是**服务器当前真态**，不是昨天的聊天记录。

## 版本基线（导入时核对）
- DSH：0.1.5-rc.2（发布包 dsh-v0.1.5-rc.2 系）
- 插件：0.1.0
- 工程服务：dsh-sim 0.1.0（STAR 适配器构建 starccm-cli-49.0.0 + STAR-CCM+ 2402 19.02.009-R8）

## 实证记录（2026-09-21 本机）
- `sim-bridge` loader entry 解析通过；`/dsh-sim/health` 返回 upstream_ok:true
- 面板页 `/dsh-sim/panels/executor/index.html` 200；API `/dsh-sim/api/tasks` 200（身份注入）
- 云图 artifact `/dsh-sim/api/artifacts/{id}/content` 200 image/png（项目头透传）
- 跨项目读 artifact 被 403（安全行为正确）；客户端 UI 视觉验收 NOT_RUN（需浏览器人工目检）

