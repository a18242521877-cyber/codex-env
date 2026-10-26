const NAV = [
  ["overview", "总览"],
  ["campaigns", "推广计划"],
  ["images", "测图"],
  ["goods", "测款"],
  ["summary", "每日总结"],
  ["plans", "调整方案"],
  ["alerts", "预警"],
  ["logs", "操作日志"],
  ["settings", "设置"],
];

const TITLES = Object.fromEntries(NAV);
const PRIORITY = { high: "高", medium: "中", low: "低" };
const STATUS = { pending: "待处理", done: "已执行", dismissed: "已忽略" };
const PACE = { fast: "消耗偏快", slow: "消耗偏慢" };

const state = {
  session: null,
  view: "overview",
  date: null,
  today: null,
  bounds: null,
  unread: 0,
  settings: null,
  plan: null,
  syncing: false,
};
let pollGen = 0;

const app = document.getElementById("app");
app.addEventListener("click", onClick);
app.addEventListener("change", onChange);
app.addEventListener("submit", onSubmit);
window.addEventListener("hashchange", () => {
  const next = location.hash.replace(/^#\/?/, "");
  if (TITLES[next] && state.session?.logged_in) {
    state.view = next;
    renderShell();
    loadView();
  }
});

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function money(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function roi(value) {
  return value == null ? "—" : Number(value).toFixed(2);
}
function rate(value) {
  return value == null ? "—" : `${(Number(value) * 100).toFixed(2)}%`;
}
function dod(value) {
  if (value == null) return '<span class="dod flat">—</span>';
  const cls = value > 0.005 ? "up" : value < -0.005 ? "down" : "flat";
  const sign = value > 0 ? "+" : "";
  return `<span class="dod ${cls}">${sign}${(value * 100).toFixed(1)}%</span>`;
}
function kpiText(kpi) {
  if (kpi.value == null) return "—";
  if (kpi.format === "rate") return rate(kpi.value);
  if (kpi.format === "roi") return roi(kpi.value);
  if (kpi.format === "count") return String(kpi.value);
  return money(kpi.value);
}
function when(iso) {
  if (!iso) return "还没有同步";
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return iso;
  return time.toLocaleString("zh-CN", { hour12: false });
}

function shiftDay(isoDate, delta) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  date.setDate(date.getDate() + delta);
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${mm}-${dd}`;
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const headers = { "X-Pdd-Monitor": "1" };
  const init = { method: options.method || "GET", headers };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get("content-type") || "";
  if (!response.ok) {
    let message = "请求失败";
    if (type.includes("json")) {
      const payload = await response.json();
      message = payload.error || message;
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (type.includes("json")) return response.json();
  return response.text();
}

function stopPoll() {
  pollGen += 1;
}

function manualLogin(gate) {
  return `请打开${gate}，在浏览器里自己完成登录，回到推广台后再点「同步」。同步只读取已保存的登录，不会代点，也不会等待扫码。`;
}

function loginGates() {
  const gates = state.settings && state.settings.login_gates;
  return Array.isArray(gates) && gates.length ? gates : [
    { id: "mms", label: "商家后台", url: "https://mms.pinduoduo.com/login/" },
    { id: "yingxiao", label: "推广中心", url: "https://yingxiao.pinduoduo.com/mains/promotionOverview" },
  ];
}

function gateButtons(withLinks) {
  return loginGates().map((gate) => `
    <button class="btn" type="button" data-act="open-gate" data-gate="${esc(gate.id)}">打开${esc(gate.label)}</button>
    ${withLinks ? `<a class="btn ghost" href="${esc(gate.url)}" target="_blank" rel="noreferrer">${esc(gate.label)}地址</a>` : ""}
  `).join("");
}

async function boot() {
  stopPoll();
  try {
    state.session = await api("/api/session");
  } catch (error) {
    app.textContent = error.message;
    return;
  }
  if (!state.session.logged_in) {
    state.settings = await api("/api/settings");
    renderLogin();
    if (state.settings.values.data_mode === "mock") startQr();
    else renderLiveGates();
    return;
  }
  const info = await api("/api/bootstrap");
  state.today = info.today;
  state.bounds = info.bounds;
  state.unread = info.unread_alerts;
  state.lastSync = info.last_sync;
  if (!state.date) state.date = localStorage.getItem("pddmon-date") || info.today;
  if (state.date > info.today) state.date = info.today;
  if (state.bounds && state.date < state.bounds.min) state.date = state.bounds.min;
  const hash = location.hash.replace(/^#\/?/, "");
  if (TITLES[hash]) state.view = hash;
  renderShell();
  await loadView();
}

function renderLogin() {
  const mode = state.settings.values.data_mode;
  app.innerHTML = `
    <section class="login">
      <div class="login-copy">
        <div class="brand">
          <div class="brand-mark">推</div>
          <div><strong>推广台</strong><span>拼多多店铺</span></div>
        </div>
        <h1>看清推广、测图和测款，当天就能决定怎么调。</h1>
        <p class="lead">打开登录页并自己完成后，点「同步」查看花费、成交和 ROI，再生成每日总结与调整方案。数据留在这台电脑上。</p>
        <div class="points">
          <div><strong>测图</strong><span class="muted"> 比较主图点击率和转化率，样本够了再建议换图。</span></div>
          <div><strong>测款</strong><span class="muted"> 按近 7 个完整日判断放量、暂停，或先改详情。</span></div>
          <div><strong>每日总结</strong><span class="muted"> 中文复盘加可勾选的调整清单，能导出。</span></div>
        </div>
      </div>
      <div class="login-card" id="login-panel">
        <div class="mode-switch">
          <button class="btn ${mode === "mock" ? "primary" : ""}" data-act="mode" data-mode="mock" type="button">模拟店铺</button>
          <button class="btn ${mode === "live" ? "primary" : ""}" data-act="mode" data-mode="live" type="button">真实店铺</button>
        </div>
        <p class="small muted">正在准备登录…</p>
      </div>
    </section>`;
}

async function startQr() {
  const gen = ++pollGen;
  const panel = document.getElementById("login-panel");
  if (!panel) return;
  let data;
  try {
    data = await api("/api/auth/qr", { method: "POST", body: {} });
  } catch (error) {
    panel.innerHTML = `<p>${esc(error.message)}</p>`;
    return;
  }
  if (gen !== pollGen || !document.getElementById("login-panel")) return;
  const mode = state.settings.values.data_mode;
  const switcher = `
    <div class="mode-switch">
      <button class="btn ${mode === "mock" ? "primary" : ""}" data-act="mode" data-mode="mock" type="button">模拟店铺</button>
      <button class="btn ${mode === "live" ? "primary" : ""}" data-act="mode" data-mode="live" type="button">真实店铺</button>
    </div>`;
  if (!data.ready) {
    panel.innerHTML = `
      ${switcher}
      <h2>还不能登录</h2>
      <p class="small">${esc(data.message || "请先装好本机浏览器组件")}</p>
      ${data.official_login_url ? `<p><a href="${esc(data.official_login_url)}" target="_blank" rel="noreferrer">打开拼多多商家后台登录页</a></p>` : ""}`;
    return;
  }
  state.ticket = data.ticket;
  if (data.mode === "live") {
    panel.innerHTML = liveGateMarkup(switcher, data.message || data.hint || "");
    const status = document.getElementById("login-status");
    if (status) status.textContent = data.message || data.hint || "";
    poll(gen, data.ticket);
    return;
  }
  panel.innerHTML = `
    ${switcher}
    <div class="qr-wrap" id="qr-slot"></div>
    <p class="small">${esc(data.hint)}</p>
    <div class="actions">
      <button class="btn primary" type="button" data-act="confirm" id="btn-mock-confirm">模拟已在手机确认</button>
      <button class="btn ghost" type="button" data-act="refresh-qr">刷新二维码</button>
    </div>`;
  const slot = document.getElementById("qr-slot");
  if (slot) slot.innerHTML = data.qr_svg;
  poll(gen, data.ticket);
}

function liveGateMarkup(switcher, message) {
  return `
    ${switcher}
    <h2>自己完成登录</h2>
    <p class="small" id="login-status">${esc(message || "先打开要登录的页面。在弹出的浏览器里自己完成登录，回到这里再点「同步」。")}</p>
    <p class="small muted">${esc(manualLogin("商家后台、推广中心或其他登录页"))} 头像若返回「Token不合法」，那是登录失败，不是成功。营销域名上只有 _nano_fp 也不算登录。</p>
    <div class="actions">
      ${gateButtons(true)}
      ${state.session && state.session.resume_available ? '<button class="btn primary" type="button" data-act="resume-login">用已保存登录进入</button>' : ""}
    </div>`;
}

function renderLiveGates() {
  const panel = document.getElementById("login-panel");
  if (!panel) return;
  const mode = state.settings.values.data_mode;
  const switcher = `
    <div class="mode-switch">
      <button class="btn ${mode === "mock" ? "primary" : ""}" data-act="mode" data-mode="mock" type="button">模拟店铺</button>
      <button class="btn ${mode === "live" ? "primary" : ""}" data-act="mode" data-mode="live" type="button">真实店铺</button>
    </div>`;
  panel.innerHTML = liveGateMarkup(switcher, "");
}

async function poll(gen, ticket) {
  if (gen !== pollGen) return;
  try {
    const status = await api(`/api/auth/qr/${encodeURIComponent(ticket)}`);
    if (gen !== pollGen) return;
    const note = document.getElementById("login-status");
    if (note && status.message) note.textContent = status.message;
    const img = document.getElementById("live-qr");
    if (img && status.qr_image && img.getAttribute("src") !== status.qr_image) {
      img.src = status.qr_image;
      img.hidden = false;
    }
    if (status.status === "confirmed") {
      if (status.message) toast(status.message);
      await boot();
      return;
    }
    if (status.status === "failed") {
      if (note) note.textContent = status.message || "登录没有完成";
      toast(status.message || "登录失败");
      showResume(status);
      return;
    }
    if (status.status === "expired") {
      if (img) {
        if (note) note.textContent = status.message || "二维码已过期。若已经扫过，点「用已保存登录进入」。";
        showResume(status);
        return;
      }
      toast("二维码已过期，已刷新");
      startQr();
      return;
    }
  } catch (error) {
    if (gen === pollGen) toast(error.message);
    return;
  }
  setTimeout(() => poll(gen, ticket), 1500);
}

function showResume(status) {
  const actions = document.querySelector("#login-panel .actions");
  if (!actions || !status.resume_available || document.getElementById("resume-slot")) return;
  actions.insertAdjacentHTML(
    "afterbegin",
    '<button class="btn primary" type="button" data-act="resume-login" id="resume-slot">用已保存登录进入</button>',
  );
}

function renderShell() {
  const shop = state.session.shop;
  const nav = NAV.map(([id, label]) => {
    const badge = id === "alerts" && state.unread ? `<span class="badge">${state.unread}</span>` : "";
    return `<button type="button" data-act="nav" data-view="${id}" class="${state.view === id ? "active" : ""}">${label}${badge}</button>`;
  }).join("");
  const min = state.bounds ? `min="${esc(state.bounds.min)}"` : "";
  const max = state.today ? `max="${esc(state.today)}"` : "";
  app.innerHTML = `
    <div class="shell">
      <aside class="sidebar">
        <div class="brand"><div class="brand-mark">推</div><div><strong>推广台</strong><span>${esc(shop.name)}</span></div></div>
        <nav class="nav">${nav}</nav>
        <div class="aside-foot">${state.session.mode === "live" ? "真实店铺" : "模拟店铺"} ${esc(shop.mall_id)}<br>最近同步 ${esc(when(state.lastSync?.finished_at))}</div>
      </aside>
      <main class="main">
        <header class="topbar">
          <div>
            <h1>${esc(TITLES[state.view])}</h1>
            <p id="view-note"></p>
          </div>
          <div class="tools">
            <input id="stat-date" type="date" value="${esc(state.date)}" ${min} ${max} />
            <button class="btn" type="button" data-act="yesterday">昨天</button>
            <button class="btn" type="button" data-act="sync" ${state.syncing ? "disabled" : ""}>同步</button>
            ${state.session.mode === "live" ? gateButtons(false) : ""}
            <button class="btn ghost" type="button" data-act="logout">退出</button>
          </div>
        </header>
        <section id="view"></section>
      </main>
    </div>`;
}

async function loadView() {
  const seq = ++loadView.seq;
  const view = document.getElementById("view");
  const note = document.getElementById("view-note");
  if (!view) return;
  view.innerHTML = `<p class="muted">正在读取…</p>`;
  const date = encodeURIComponent(state.date);
  try {
    if (state.view === "overview") {
      const data = await api(`/api/dashboard?date=${date}`);
      if (seq !== loadView.seq) return;
      note.textContent = data.complete ? "这一天已经结束。" : "今天尚未结束，环比仍然对照前一天。";
      view.innerHTML = renderOverview(data);
    } else if (state.view === "campaigns") {
      const data = await api(`/api/campaigns?date=${date}`);
      if (seq !== loadView.seq) return;
      note.textContent = "计划口径。模拟数据中，计划花费等于名下测款之和。";
      view.innerHTML = renderCampaigns(data.items);
    } else if (state.view === "images") {
      const data = await api(`/api/image-tests?date=${date}`);
      if (seq !== loadView.seq) return;
      note.textContent = "创意口径，不计入店铺合计。换图建议看近 7 个完整日。";
      view.innerHTML = renderImages(data.items);
    } else if (state.view === "goods") {
      const data = await api(`/api/goods-tests?date=${date}`);
      if (seq !== loadView.seq) return;
      note.textContent = "商品口径。店铺合计和每日总结都用这一层，避免和计划重复相加。";
      view.innerHTML = renderGoods(data.items);
    } else if (state.view === "summary") {
      const data = await api(`/api/summary?date=${date}`);
      if (seq !== loadView.seq) return;
      note.textContent = data.complete ? "完整日总结。" : "日内参考，全天结论请切到昨天。";
      view.innerHTML = renderSummary(data);
    } else if (state.view === "plans") {
      const data = await api(`/api/plans?date=${date}`);
      if (seq !== loadView.seq) return;
      state.plan = data;
      note.textContent = `规则窗口 ${data.window_start} 至 ${data.window_end}`;
      view.innerHTML = renderPlan(data);
    } else if (state.view === "alerts") {
      const data = await api("/api/alerts");
      if (seq !== loadView.seq) return;
      state.unread = data.unread;
      renderShell();
      if (seq !== loadView.seq) return;
      document.getElementById("view-note").textContent = `未读 ${data.unread} 条。高优先级来自调整方案。`;
      document.getElementById("view").innerHTML = renderAlerts(data.items);
    } else if (state.view === "logs") {
      const data = await api("/api/logs");
      if (seq !== loadView.seq) return;
      note.textContent = "只记录在这台电脑上的操作，不含登录票据。";
      view.innerHTML = renderLogs(data.items);
    } else if (state.view === "settings") {
      state.settings = await api("/api/settings");
      if (seq !== loadView.seq) return;
      note.textContent = "阈值只影响本机建议，不会改拼多多后台。";
      view.innerHTML = renderSettings(state.settings);
    }
  } catch (error) {
    if (seq !== loadView.seq) return;
    if (error.status === 401) {
      await boot();
      return;
    }
    view.innerHTML = `<p class="banner">${esc(error.message)}</p>`;
  }
}
loadView.seq = 0;

function renderKpis(kpis) {
  return `<div class="kpi-grid">${kpis.map((kpi) => `
    <article class="kpi">
      <div class="label">${esc(kpi.label)}</div>
      <div class="value num">${esc(kpiText(kpi))}${kpi.unit && kpi.format !== "rate" && kpi.format !== "roi" ? `<span class="unit">${esc(kpi.unit)}</span>` : ""}</div>
      <div class="small">环比 ${dod(kpi.dod)}</div>
    </article>`).join("")}</div>`;
}

function renderOverview(data) {
  const maxSpend = Math.max(...data.trend.map((row) => row.spend_yuan), 1);
  const width = data.trend.length * 36;
  const bars = data.trend.map((row, index) => {
    const height = Math.max(4, (row.spend_yuan / maxSpend) * 110);
    const good = row.roi != null && row.roi >= data.target_roi;
    return `<rect class="${good ? "bar-good" : "bar-bad"}" x="${index * 36 + 6}" y="${124 - height}" width="22" height="${height}" rx="4"><title>${esc(row.date)} 花费 ${money(row.spend_yuan)} ROI ${roi(row.roi)}</title></rect>`;
  }).join("");
  const preview = data.plan_preview.length
    ? data.plan_preview.map((item) => `<li><strong>${esc(item.title)}</strong><div class="muted small">${esc(item.suggestion)}</div></li>`).join("")
    : `<li>这一窗口没有待处理的中高优先级建议。</li>`;
  const pacing = data.campaigns.map((row) => {
    const util = Math.max(0, Math.min(1, row.budget_util || 0));
    const flag = PACE[row.pace_flag] || (data.complete ? "全天" : "进行中");
    return `<div class="split small"><span>${esc(row.name)}</span><span class="num">${esc(flag)} ${rate(row.budget_util)}</span></div>
      <div class="bar ${row.pace_flag === "fast" ? "bad" : row.pace_flag === "slow" ? "" : "good"}"><span style="width:${(util * 100).toFixed(1)}%"></span></div>`;
  }).join("");
  return `
    ${data.complete ? "" : '<div class="banner">今天的数据还会变。总览按当前累计展示，调整方案里的测款和测图不含今天。</div>'}
    ${renderKpis(data.kpis)}
    <div class="layout">
      <section class="panel">
        <h2>近 14 日花费</h2>
        <svg class="trend" viewBox="0 0 ${width} 140" role="img" aria-label="花费趋势">${bars}</svg>
        <div class="trend-labels"><span>${esc(data.trend[0] ? data.trend[0].date : "")}</span><span>达标 ROI 为绿色，低于 ${esc(roi(data.target_roi))} 为橙色</span><span>${esc(data.trend.length ? data.trend[data.trend.length - 1].date : "")}</span></div>
      </section>
      <section class="panel">
        <h2>预算节奏</h2>
        ${pacing || '<p class="empty">没有计划</p>'}
        <h2>待处理</h2>
        <ul class="list">${preview}</ul>
        <p class="small muted">建议基于 ${esc(data.plan_date || "—")}。</p>
      </section>
    </div>`;
}

function renderCampaigns(items) {
  if (!items.length) return `<p class="empty">没有推广计划。</p>`;
  const rows = items.map((row) => `
    <tr>
      <td class="name">${esc(row.name)}<div class="small muted">${esc(row.scene)}</div></td>
      <td><span class="pill ${row.status === "投放中" ? "good" : "warn"}">${esc(row.status)}</span></td>
      <td class="num">${money(row.spend_yuan)}</td>
      <td class="num">${money(row.budget_yuan)}</td>
      <td><div class="bar"><span style="width:${Math.min(100, (row.budget_util || 0) * 100).toFixed(1)}%"></span></div></td>
      <td class="num">${roi(row.roi)}</td>
      <td class="num">${rate(row.ctr)}</td>
      <td class="num">${row.orders}</td>
      <td class="num">${row.bid_yuan == null ? "—" : money(row.bid_yuan)}</td>
      <td>${esc(PACE[row.pace_flag] || "—")}</td>
    </tr>`).join("");
  return `<div class="panel table-wrap"><table>
    <thead><tr><th>计划</th><th>状态</th><th>花费</th><th>日预算</th><th>使用率</th><th>ROI</th><th>点击率</th><th>订单</th><th>出价</th><th>节奏</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderImages(items) {
  if (!items.length) {
    const live = state.session && state.session.mode === "live";
    const last = (state.lastSync && state.lastSync.message) || "";
    let text = "没有测图。";
    if (live && last.includes("Token不合法")) {
      text = "头像登录失败：Token不合法，测图未写入。不要只点头像。" + manualLogin("推广中心页面");
    } else if (live && (last.includes("登录态为空") || last.includes("尚未登录") || last.includes("测图测款未写入"))) {
      text = "推广中心尚未登录，测图未写入。" + manualLogin("推广中心页面");
    } else if (live) {
      text = "测图需要推广报表里的创意或主图。" + manualLogin("推广报表页面");
    }
    return `<p class="empty">${text}</p>`;
  }
  const maxCtr = Math.max(...items.map((row) => row.ctr || 0), 0.01);
  return `<div class="cards">${items.map((row) => `
    <article class="image-card">
      <div class="swatch ${esc(row.tone)}">${esc(row.label)}</div>
      <h3>${esc(row.label)}</h3>
      <p class="small muted">${esc(row.goods_name)}</p>
      <div class="bar"><span style="width:${((row.ctr || 0) / maxCtr * 100).toFixed(1)}%"></span></div>
      <p class="split small"><span>点击率 ${rate(row.ctr)}</span><span>转化率 ${rate(row.cvr)}</span></p>
      <p class="split small muted"><span>曝光 ${row.impressions}</span><span>订单 ${row.orders}</span></p>
    </article>`).join("")}</div>`;
}

function renderGoods(items) {
  if (!items.length) return `<p class="empty">没有测款。</p>`;
  const shopData = items.filter((row) => row.category === "商品数据");
  const homeSpend = shopData.some((row) => row.name === "首页推广花费");
  const banner = shopData.length && shopData.length === items.length
    ? (homeSpend
      ? `<p class="banner">首页上的推广花费已写入。这是首页汇总，不是测款或测图。${esc(manualLogin("推广中心页面"))}</p>`
      : `<p class="banner">这些成交来自商家后台「商品数据」，不是推广测款。${esc(manualLogin("推广中心页面"))}</p>`)
    : shopData.length
      ? `<p class="banner">标成「商品数据」的是全店成交或首页花费兜底，不计入推广测款。</p>`
      : "";
  return `${banner}<div class="cards">${items.map((row) => `
    <article class="goods-card">
      <div class="split"><span class="pill">${esc(row.stage)}</span><span class="small muted">${esc(row.category)}</span></div>
      <h3>${esc(row.name)}</h3>
      <p class="small muted">${esc(row.campaign_name)} · 标价 ${money(row.price_yuan)} 元</p>
      <p class="split"><span class="num">ROI ${roi(row.roi)}</span><span class="num">${money(row.spend_yuan)} 元</span></p>
      <div class="bar ${row.roi != null && row.roi < 1 ? "bad" : "good"}"><span style="width:${Math.min(100, (row.budget_util || 0) * 100).toFixed(1)}%"></span></div>
      <p class="small muted">预算使用 ${rate(row.budget_util)} · 日预算 ${money(row.budget_yuan)} 元</p>
      <p class="split small"><span>点击率 ${rate(row.ctr)}</span><span>转化率 ${rate(row.cvr)}</span></p>
      <p class="split small"><span>订单 ${row.orders}</span><span>加购 ${row.add_to_cart}</span></p>
    </article>`).join("")}</div>`;
}

function renderSummary(data) {
  const top = data.top_goods.map((row) => `<li>${esc(row.name)} · ROI ${roi(row.roi)} · ${row.orders} 单</li>`).join("") || "<li>无</li>";
  const weak = data.weak_goods.map((row) => `<li>${esc(row.name)} · ROI ${roi(row.roi)}</li>`).join("") || "<li>没有低于目标的测款</li>";
  return `
    ${data.complete ? "" : '<div class="banner">今天还没过完。这份总结只反映截至当前的数字。</div>'}
    ${renderKpis(data.kpis)}
    <div class="two">
      <section class="panel"><h2>亮点</h2><ul class="list">${data.highlights.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></section>
      <section class="panel"><h2>风险</h2><ul class="list">${data.risks.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></section>
    </div>
    <section class="panel" style="margin-top:12px">
      <div class="split">
        <h2>${esc(data.headline)}</h2>
        <div class="actions">
          <a class="btn" href="/api/export/daily.md?date=${esc(state.date)}">导出 Markdown</a>
          <a class="btn" href="/api/export/metrics.csv?date=${esc(state.date)}">导出 CSV</a>
          <button class="btn ghost" type="button" data-act="print">打印</button>
        </div>
      </div>
      <div class="narrative">${esc(data.narrative)}</div>
    </section>
    <div class="two" style="margin-top:12px">
      <section class="panel"><h2>效率靠前</h2><ul class="list">${top}</ul></section>
      <section class="panel"><h2>低于目标</h2><ul class="list">${weak}</ul></section>
    </div>`;
}

function renderPlan(data) {
  const groups = ["high", "medium", "low"].map((level) => {
    const items = data.items.filter((item) => item.priority === level);
    if (!items.length) return "";
    return `<h2 class="block-title">${PRIORITY[level]}优先级</h2><div class="plan-list">${items.map(renderPlanItem).join("")}</div>`;
  }).join("");
  return `
    <div class="banner info">${esc(data.overview)}</div>
    <div class="actions" style="margin-bottom:12px">
      <button class="btn" type="button" data-act="copy-plan">复制方案</button>
      <button class="btn ghost" type="button" data-act="refresh-plan">重新生成并记入日志</button>
      <a class="btn ghost" href="/api/export/daily.md?date=${esc(state.date)}">导出日报</a>
    </div>
    ${groups || '<p class="empty">这一天没有建议。</p>'}
    <p class="small muted">${esc(data.footnote)}</p>`;
}

function renderPlanItem(item) {
  const chips = (item.chips || []).map((chip) => `<span>${esc(chip.label)} ${esc(chip.value)}</span>`).join("");
  const buttons = item.status === "pending"
    ? `<button class="btn small primary" type="button" data-act="status" data-key="${esc(item.item_key)}" data-status="done">标记已执行</button>
       <button class="btn small ghost" type="button" data-act="status" data-key="${esc(item.item_key)}" data-status="dismissed">忽略</button>`
    : `<button class="btn small ghost" type="button" data-act="status" data-key="${esc(item.item_key)}" data-status="pending">恢复为待处理</button>`;
  return `<article class="plan-item">
    <header><span class="pill ${item.priority === "high" ? "bad" : item.priority === "medium" ? "warn" : "muted"}">${esc(item.category)}</span>
      <span class="pill muted">${STATUS[item.status]}</span></header>
    <h3>${esc(item.title)}</h3>
    <p>${esc(item.reason)}</p>
    <p><strong>${esc(item.suggestion)}</strong></p>
    <div class="chips">${chips}</div>
    <div class="actions">${buttons}</div>
  </article>`;
}

function renderAlerts(items) {
  if (!items.length) return `<p class="empty">没有预警。</p>`;
  const rows = items.map((row) => `
    <tr>
      <td>${esc(row.stat_date)}</td>
      <td><span class="pill ${row.level === "warning" ? "bad" : "warn"}">${row.level === "warning" ? "重要" : "留意"}</span></td>
      <td class="name">${esc(row.title)}<div class="small muted">${esc(row.body)}</div></td>
      <td>${row.read_at ? "已读" : `<button class="btn small" type="button" data-act="read-one" data-date="${esc(row.stat_date)}" data-code="${esc(row.code)}">标为已读</button>`}</td>
    </tr>`).join("");
  return `<div class="actions" style="margin-bottom:12px"><button class="btn" type="button" data-act="read-all">全部已读</button></div>
    <div class="panel table-wrap"><table><thead><tr><th>日期</th><th>级别</th><th>内容</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderLogs(items) {
  if (!items.length) return `<p class="empty">还没有操作。</p>`;
  const rows = items.map((row) => `<tr><td>${esc(when(row.created_at))}</td><td>${esc(row.action)}</td><td class="name">${esc(row.detail)}</td></tr>`).join("");
  return `<div class="panel table-wrap"><table><thead><tr><th>时间</th><th>动作</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderSettings(payload) {
  const groups = [];
  payload.fields.forEach((field) => {
    if (!groups.length || groups[groups.length - 1].name !== field.group) {
      groups.push({ name: field.group, fields: [] });
    }
    groups[groups.length - 1].fields.push(field);
  });
  const form = groups.map((group) => `
    <h3 class="group-title">${esc(group.name)}</h3>
    ${group.fields.map((field) => {
      const value = payload.values[field.key] ?? "";
      let control = "";
      if (field.type === "enum") {
        const options = field.choices.map((choice) => `<option value="${esc(choice.value)}" ${choice.value === value ? "selected" : ""}>${esc(choice.label)}</option>`).join("");
        control = `<select data-setting="${esc(field.key)}">${options}</select>`;
      } else {
        const step = field.type === "int" ? "1" : "any";
        control = `<input data-setting="${esc(field.key)}" type="number" step="${step}" min="${field.min}" max="${field.max}" value="${esc(value)}" />`;
      }
      return `<div class="field"><label>${esc(field.label)}</label>${control}<div class="help">${esc(field.help)}</div></div>`;
    }).join("")}`).join("");
  return `<form id="settings-form" class="panel">
      <div class="settings-grid">${form}</div>
      <div class="actions"><button class="btn primary" type="submit">保存设置</button></div>
    </form>
    <section class="panel glossary">
      <h2>口径</h2>
      <ul class="list">
        <li>店铺合计用测款日报。推广计划单独看，模拟数据里两者按商品归集后一致。</li>
        <li>测图是创意层，不加进店铺花费，避免一张图把商品花费算两次。</li>
        <li>ROI = 成交额 / 花费。点击率 = 点击 / 曝光。转化率 = 订单 / 点击。</li>
        <li>模拟成交额 = 订单 × 标价。真实店铺用报表里的交易额。表格按元；接口里的整数按分、小数按元。若和后台差 100 倍，以后台为准。</li>
        <li>调整方案看最近 7 个完整日。查看今天时，测款和测图仍截止到昨天；只有预算节奏用今天的花费。</li>
      </ul>
      <h2>本地数据</h2>
      <p class="small">退出登录不会删历史。下面的操作会删掉店铺、报表、日报和建议，设置保留。</p>
      <button class="btn danger" type="button" data-act="reset">清空本地数据</button>
    </section>`;
}

function planPlainText(plan) {
  const lines = [`调整方案 ${plan.date}`, plan.overview, ""];
  plan.items.forEach((item) => {
    lines.push(`【${PRIORITY[item.priority]}｜${STATUS[item.status]}】${item.title}`);
    lines.push(item.suggestion);
    lines.push("");
  });
  lines.push(plan.footnote);
  return lines.join("\n");
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
}

async function onClick(event) {
  const node = event.target.closest("[data-act]");
  if (!node) return;
  const act = node.dataset.act;
  try {
    if (act === "nav") {
      state.view = node.dataset.view;
      history.replaceState(null, "", `#/${state.view}`);
      renderShell();
      await loadView();
    } else if (act === "mode") {
      state.settings = await api("/api/settings", { method: "PUT", body: { data_mode: node.dataset.mode } });
      renderLogin();
      if (node.dataset.mode === "mock") startQr();
      else renderLiveGates();
    } else if (act === "refresh-qr" || act === "resume-login") {
      startQr();
    } else if (act === "open-gate") {
      const opened = await api("/api/auth/open", { method: "POST", body: { gate: node.dataset.gate } });
      state.ticket = opened.ticket;
      const status = document.getElementById("login-status");
      if (status) status.textContent = opened.message || opened.hint || "";
      else toast(opened.message || "已打开登录页");
      if (opened.ticket) poll(++pollGen, opened.ticket);
    } else if (act === "confirm") {
      node.disabled = true;
      await api(`/api/auth/qr/${encodeURIComponent(state.ticket)}/confirm`, { method: "POST", body: {} });
      await boot();
    } else if (act === "logout") {
      await api("/api/auth/logout", { method: "POST", body: {} });
      state.date = null;
      await boot();
    } else if (act === "yesterday") {
      const iso = shiftDay(state.today, -1);
      state.date = state.bounds && iso < state.bounds.min ? state.bounds.min : iso;
      localStorage.setItem("pddmon-date", state.date);
      renderShell();
      await loadView();
    } else if (act === "sync") {
      state.syncing = true;
      renderShell();
      const result = await api("/api/sync", { method: "POST", body: {} });
      toast(result.message);
      const info = await api("/api/bootstrap");
      state.lastSync = info.last_sync;
      state.unread = info.unread_alerts;
      state.bounds = info.bounds;
      state.syncing = false;
      renderShell();
      await loadView();
    } else if (act === "status") {
      await api("/api/plans/status", {
        method: "POST",
        body: { date: state.date, item_key: node.dataset.key, status: node.dataset.status },
      });
      await loadView();
    } else if (act === "refresh-plan") {
      state.plan = await api(`/api/plans/refresh?date=${encodeURIComponent(state.date)}`, { method: "POST", body: { date: state.date } });
      toast("已重新生成");
      document.getElementById("view").innerHTML = renderPlan(state.plan);
    } else if (act === "copy-plan") {
      if (!state.plan) return;
      await copyText(planPlainText(state.plan));
      toast("调整方案已复制");
    } else if (act === "print") {
      window.print();
    } else if (act === "read-all") {
      await api("/api/alerts/read", { method: "POST", body: { all: true } });
      await loadView();
    } else if (act === "read-one") {
      await api("/api/alerts/read", { method: "POST", body: { date: node.dataset.date, code: node.dataset.code } });
      await loadView();
    } else if (act === "reset") {
      const typed = window.prompt("将删除本地店铺、报表、日报和建议。设置会保留。请输入：清空");
      if (typed !== "清空") return;
      await api("/api/data/reset", { method: "POST", body: { confirm: "清空" } });
      toast("本地数据已清空");
      state.date = null;
      await boot();
    }
  } catch (error) {
    state.syncing = false;
    toast(error.message);
    if (error.status === 401) await boot();
  }
}

function onChange(event) {
  if (event.target.id !== "stat-date") return;
  state.date = event.target.value;
  localStorage.setItem("pddmon-date", state.date);
  loadView();
}

async function onSubmit(event) {
  if (event.target.id !== "settings-form") return;
  event.preventDefault();
  const body = {};
  event.target.querySelectorAll("[data-setting]").forEach((input) => {
    body[input.dataset.setting] = input.value;
  });
  try {
    state.settings = await api("/api/settings", { method: "PUT", body });
    toast("设置已保存");
    if (state.session?.logged_in) await loadView();
  } catch (error) {
    toast(error.message);
  }
}

boot();
