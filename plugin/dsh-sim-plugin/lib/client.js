/**
 * dsh-sim-plugin — 客户端入口（DSH 0.1.5-rc.2 web 端）
 *
 * 三个槽位（全部官方先例）：
 *  - sidebar.right.pane.tab / .title → 「仿真工作台」右侧常驻面板（iframe 同源加载工程面板）
 *  - settings.plugin.item → 「仿真服务」设置卡片（apiUrl/身份/连通自检/能力包清单）
 *
 * React 18 + jsx-runtime 由宿主 window.__ModuleLoader__ 提供（与 dsh-visual-plugin 0.3.2 同机制），
 * 本文件零打包依赖，离线可分发。
 */
window.__ModuleLoader__.load({
  id: "dsh-sim-plugin",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const react = require("react");
    const jsx = require("react/jsx-runtime");
    const jsxRuntime = { jsx: jsx.jsx, jsxs: jsx.jsxs, Fragment: jsx.Fragment };

    const NS = "dsh-sim";
    const TAB_ID = "sim-workbench";

    const zh = {
      "tab.title": "仿真工作台",
      "view.executor": "执行台",
      "view.reviewer": "审查台",
      "view.monitor": "监控",
      "health.ok": "工程服务在线",
      "health.bad": "工程服务不可用",
      "settings.title": "仿真服务",
      "settings.apiUrl": "工程 API 地址",
      "settings.subject": "身份（开发模式）",
      "settings.roles": "角色",
      "settings.test": "测试连接",
      "settings.testing": "检测中…",
      "settings.capabilities": "能力包",
      "frame.openExternal": "在浏览器打开",
      "frame.hint": "深链：任务 / 审查 / 证据均可从此面板直达",
    };
    const en = {
      "tab.title": "Sim Workbench",
      "view.executor": "Executor",
      "view.reviewer": "Reviewer",
      "view.monitor": "Monitor",
      "health.ok": "Engineering API online",
      "health.bad": "Engineering API unavailable",
      "settings.title": "Simulation Service",
      "settings.apiUrl": "Engineering API URL",
      "settings.subject": "Identity (dev)",
      "settings.roles": "Roles",
      "settings.test": "Test connection",
      "settings.testing": "Testing…",
      "settings.capabilities": "Capability packages",
      "frame.openExternal": "Open in browser",
      "frame.hint": "Deep links: tasks / reviews / evidence",
    };

    const CSS = {
      wrap: "display:flex;flex-direction:column;height:100%;min-height:0;background:var(--dsw-alias-bg-base,#fff);color:var(--dsw-alias-label-primary,#0f1115)",
      topbar: "display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--dsw-alias-border-l2,#0000001a);flex:none",
      segbtn: "font:inherit;font-size:12px;padding:5px 12px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2,#0000001a);background:var(--dsw-alias-bg-module-platform,#f1f2f4);color:var(--dsw-alias-label-secondary,#52565c);cursor:pointer",
      segbtnActive: "font:inherit;font-size:12px;padding:5px 12px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2,#0000001a);background:var(--dsw-alias-bg-layer-2,#fff);color:var(--dsw-alias-label-primary,#111318);box-shadow:0 1px 3px #0000001a;font-weight:600",
      badge: (kind) => `font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;background:${kind === "ok" ? "#e4f4ea" : "#fdeeee"};color:${kind === "ok" ? "#1a7f4b" : "#c53d3d"}`,
      spacer: "flex:1",
      link: "font-size:12px;color:var(--dsw-alias-label-secondary,#4f5359);text-decoration:none;padding:4px 8px;border-radius:7px",
      iframe: "flex:1;min-height:0;width:100%;border:0;background:#f7f8fa",
      card: "display:flex;flex-direction:column;gap:10px;padding:14px 16px",
      label: "font-size:12px;color:var(--dsw-alias-label-tertiary,#62666b)",
      input: "font:inherit;font-size:13px;padding:7px 10px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2,#0000001f);background:var(--dsw-alias-bg-base,#fff);color:var(--dsw-alias-label-primary,#0f1115);width:100%;box-sizing:border-box",
      btn: "font:inherit;font-size:13px;padding:7px 14px;border-radius:9px;border:0;background:var(--dsw-alias-state-business-primary,#4176e6);color:#fff;cursor:pointer;align-self:flex-start",
      kv: "font-size:12px;color:var(--dsw-alias-label-secondary,#52565c);display:flex;justify-content:space-between;gap:12px",
      mono: "font-family:var(--ds-font-family-code,monospace);font-size:11px",
    };

    function SimWorkbenchBody(props) {
      const { t } = props;
      const [view, setView] = react.useState("executor");
      const [health, setHealth] = react.useState(null);
      const paths = {
        // ?api=/dsh-sim/api：面板内的 fetch 全部走同源代理（免 CORS），代理统一注入身份头
        executor: "/dsh-sim/panels/executor/index.html?api=" + encodeURIComponent("/dsh-sim/api"),
        reviewer: "/dsh-sim/panels/reviewer/index.html?api=" + encodeURIComponent("/dsh-sim/api"),
        monitor: "/dsh-sim/panels/monitor/index.html?api=" + encodeURIComponent("/dsh-sim/api"),
      };
      react.useEffect(() => {
        let alive = true;
        const probe = async () => {
          try {
            const r = await fetch("/dsh-sim/health");
            const j = await r.json();
            if (alive) setHealth(j.upstream_ok ? "ok" : "bad");
          } catch {
            if (alive) setHealth("bad");
          }
        };
        probe();
        const id = setInterval(probe, 15000);
        return () => { alive = false; clearInterval(id); };
      }, []);
      const seg = (id, labelKey) => jsxRuntime.jsx("button", {
        type: "button", style: { cssText: undefined }, className: undefined,
        onClick: () => setView(id),
        style: { },
        children: t(labelKey),
      });
      return jsxRuntime.jsxs("div", { style: { cssText: undefined }, className: undefined, children: [
        jsxRuntime.jsxs("div", { className: "dsh-sim-topbar", children: [
          ["executor", "reviewer", "monitor"].map((id) =>
            jsxRuntime.jsx("button", {
              key: id, type: "button", onClick: () => setView(id),
              className: id === view ? "dsh-sim-seg-active" : "dsh-sim-seg",
              children: t(`view.${id}`),
            })
          ),
          jsxRuntime.jsx("span", { style: { flex: 1 } }),
          jsxRuntime.jsx("span", {
            className: health === "ok" ? "dsh-sim-badge-ok" : "dsh-sim-badge-bad",
            children: t(health === "ok" ? "health.ok" : "health.bad"),
          }),
          jsxRuntime.jsx("a", {
            href: paths[view], target: "_blank", rel: "noopener", className: "dsh-sim-ext",
            children: t("frame.openExternal"),
          }),
        ] }),
        jsxRuntime.jsx("iframe", { src: paths[view], className: "dsh-sim-frame", title: `dsh-sim-${view}` }),
      ] });
    }

    function SimWorkbenchTitle(props) {
      const { t } = props;
      return jsxRuntime.jsxs(jsxRuntime.Fragment, { children: [
        jsxRuntime.jsx("span", { role: "img", "aria-hidden": true, children: "🧭" }),
        t("tab.title"),
      ] });
    }

    function SimSettingsCard(props) {
      const { t, settings } = props;
      const [form, setForm] = react.useState(() => {
        let stored = {};
        try {
          stored = {
            subject: window.localStorage.getItem("dshsim.subject") || undefined,
            roles: window.localStorage.getItem("dshsim.roles") || undefined,
            projects: window.localStorage.getItem("dshsim.projects") || undefined,
          };
        } catch { /* ignore */ }
        return { ...(settings?.get?.() ?? {}), ...Object.fromEntries(Object.entries(stored).filter(([, v]) => v)) };
      });
      const [testing, setTesting] = react.useState(false);
      const [result, setResult] = react.useState(null);
      const [caps, setCaps] = react.useState([]);
      const field = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
      const save = async () => {
        await settings?.set?.(form);
        // 面板 iframe 从 localStorage 读身份（dsh-sim/panels/shared/api.js 的取值链）
        try {
          window.localStorage.setItem("dshsim.subject", form.subject ?? "");
          window.localStorage.setItem("dshsim.roles", form.roles ?? "EXECUTOR");
          window.localStorage.setItem("dshsim.projects", form.projects ?? "default");
        } catch { /* ignore */ }
      };
      const test = async () => {
        setTesting(true); setResult(null);
        try {
          const r = await fetch("/dsh-sim/health");
          const j = await r.json();
          setResult(j);
          if (j.upstream_ok) {
            const cr = await fetch("/dsh-sim/capabilities?limit=20");
            const cj = await cr.json();
            setCaps(cj.items ?? []);
          }
        } catch (err) { setResult({ upstream_ok: false, detail: String(err) }); }
        setTesting(false);
      };
      return jsxRuntime.jsxs("div", { className: "dsh-sim-card", children: [
        jsxRuntime.jsx("strong", { children: t("settings.title") }),
        jsxRuntime.jsxs("label", { className: "dsh-sim-label", children: [t("settings.apiUrl"),
          jsxRuntime.jsx("input", { value: form.apiUrl ?? "", onChange: field("apiUrl"), className: "dsh-sim-input" })] }),
        jsxRuntime.jsxs("label", { className: "dsh-sim-label", children: [t("settings.subject"),
          jsxRuntime.jsx("input", { value: form.subject ?? "", onChange: field("subject"), className: "dsh-sim-input" })] }),
        jsxRuntime.jsxs("label", { className: "dsh-sim-label", children: [t("settings.roles"),
          jsxRuntime.jsxs("select", { value: form.roles ?? "EXECUTOR", onChange: field("roles"), className: "dsh-sim-input", children: [
            ["EXECUTOR", "REVIEWER", "CAPABILITY_OWNER", "NODE_ADMIN"].map((r) => jsxRuntime.jsx("option", { key: r, value: r, children: r })),
          ] })] }),
        jsxRuntime.jsxs("label", { className: "dsh-sim-label", children: ["项目（project_id）",
          jsxRuntime.jsx("input", { value: form.projects ?? "default", onChange: field("projects"), className: "dsh-sim-input" })] }),
        jsxRuntime.jsxs("div", { style: { display: "flex", gap: 8 }, children: [
          jsxRuntime.jsx("button", { type: "button", className: "dsh-sim-btn", onClick: save, children: "保存" }),
          jsxRuntime.jsx("button", { type: "button", className: "dsh-sim-btn", onClick: test, disabled: testing, children: testing ? t("settings.testing") : t("settings.test") }),
        ] }),
        result && jsxRuntime.jsxs("div", { className: result.upstream_ok ? "dsh-sim-badge-ok" : "dsh-sim-badge-bad", children: [
          result.upstream_ok ? `✅ ${t("health.ok")}` : `❌ ${t("health.bad")}`,
          jsxRuntime.jsx("div", { className: "dsh-sim-mono", children: result.detail ?? "" }),
        ] }),
        caps.length > 0 && jsxRuntime.jsxs("div", { children: [
          jsxRuntime.jsx("div", { className: "dsh-sim-label", children: t("settings.capabilities") }),
          caps.map((c) => jsxRuntime.jsxs("div", { className: "dsh-sim-kv", children: [
            jsxRuntime.jsx("span", { children: `${c.capability_package_id}@${c.version}` }),
            jsxRuntime.jsx("span", { className: c.status === "RELEASED" ? "dsh-sim-badge-ok" : "dsh-sim-badge-bad", children: c.status }),
          ] }, c.capability_package_id)),
        ] }),
      ] });
    }

    const STYLES = `
.dsh-sim-topbar{${CSS.topbar}}
.dsh-sim-seg{${CSS.segbtn}}
.dsh-sim-seg-active{${CSS.segbtnActive}}
.dsh-sim-badge-ok{${CSS.badge("ok")}}
.dsh-sim-badge-bad{${CSS.badge("bad")}}
.dsh-sim-ext{${CSS.link}}
.dsh-sim-ext:hover{background:var(--dsw-alias-interactive-bg-hover,#0000000f)}
.dsh-sim-frame{${CSS.iframe}}
.dsh-sim-card{${CSS.card}}
.dsh-sim-label{${CSS.label};display:flex;flex-direction:column;gap:4px}
.dsh-sim-input{${CSS.input}}
.dsh-sim-btn{${CSS.btn}}
.dsh-sim-btn:disabled{opacity:.6;cursor:default}
.dsh-sim-kv{${CSS.kv}}
.dsh-sim-mono{${CSS.mono};color:var(--dsw-alias-label-tertiary,#62666b);margin-top:4px}
`;

    function apply(ctx) {
      ctx.effect(() => ctx.locale.register(NS, { zh, en }), "dsh-sim: dictionaries");
      // 样式注入（一次性，无外部依赖）
      ctx.effect(() => {
        const el = document.createElement("style");
        el.dataset.dshSim = "1";
        el.textContent = STYLES;
        document.head.append(el);
        return () => el.remove();
      }, "dsh-sim: styles");
      ctx.effect(() => ctx.slots.inject("sidebar.right.pane.tab", () => ctx.slots.register({
        name: "sidebar.right.pane.tab",
        key: TAB_ID,
        locale: NS,
      }, SimWorkbenchBody)), "dsh-sim: workbench tab body");
      ctx.effect(() => ctx.slots.inject("sidebar.right.pane.tab.title", () => ctx.slots.register({
        name: "sidebar.right.pane.tab.title",
        key: TAB_ID,
        locale: NS,
      }, SimWorkbenchTitle)), "dsh-sim: workbench tab title");
      ctx.effect(() => ctx.slots.inject("settings.plugin.item", () => ctx.slots.register({
        name: "settings.plugin.item",
        key: "dsh-sim",
        locale: NS,
        inject: () => ({ settings: null }),
      }, SimSettingsCard)), "dsh-sim: settings card");
    }

    exports.apply = apply;
    return module.exports;
  },
});
