/* A股选股助手 前端 */
const APP_V = "2026-11-03.3";   // 必须与 app/config.py 的 APP_VERSION 一致
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const F_LABEL = { ep: "EP", bp: "BP", momentum: "动量", lowvol: "低波", turnover: "低换手", size: "小市值", reversal: "反转", ocf_ni: "质量", sue: "SUE", fcst_up: "预告" };

let STATE = { signal: null, settings: null, perf: null, orders: null, factorCols: false };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { raw: txt }; }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const nf = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x))
  ? "—" : Number(x).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x)) ? "—" : (x * 100).toFixed(d) + "%";
const money = (x) => (x === null || x === undefined) ? "—" : "¥" + nf(x, 0);

/* ---------------- 顶部状态 ---------------- */
async function loadStatus() {
  try {
    const s = await api("/api/status");
    const d = s.data || {};
    const cd = $("#chip-data");
    if (d.ready) {
      cd.className = "chip ok";
      cd.innerHTML = `<i class="dot"></i>数据 ${d.last_date} · ${nf(d.n_stocks)} 只`;
    } else {
      cd.className = "chip warn";
      cd.innerHTML = `<i class="dot"></i>无数据，请先更新`;
    }
    const cs = $("#chip-signal");
    if (s.signal) {
      cs.className = "chip ok";
      cs.innerHTML = `<i class="dot"></i>信号 ${s.signal.date} · ${s.signal.n} 只`;
    } else {
      cs.className = "chip";
      cs.innerHTML = `<i class="dot"></i>暂无信号`;
    }
    const cm = $("#chip-market");
    if (s.market_news && s.market_news.index !== null && s.market_news.index !== undefined) {
      const v = s.market_news.index;
      cm.className = "chip " + (v > 0.15 ? "ok" : v < -0.15 ? "warn" : "");
      cm.innerHTML = `<i class="dot"></i>市场情绪 ${v > 0 ? "+" : ""}${v.toFixed(2)}`;
    }
    if (s.job && s.job.status === "running") startPolling();
    return s;
  } catch (e) {
    $("#chip-data").className = "chip warn";
    $("#chip-data").innerHTML = `<i class="dot"></i>连接失败`;
  }
}

/* ---------------- 任务进度 ---------------- */
let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  $("#progress").classList.remove("hidden");
  pollTimer = setInterval(async () => {
    try {
      const j = await api("/api/job");
      const p = Math.round((j.progress || 0) * 100);
      $("#bar").style.width = p + "%";
      $("#pmsg").textContent = `${j.name === "update" ? "更新数据" : "生成信号"} · ${p}% · ${j.message || ""}`;
      $("#btn-update").disabled = j.status === "running";
      $("#btn-signal").disabled = j.status === "running";
      if (j.status !== "running") {
        clearInterval(pollTimer); pollTimer = null;
        $("#progress").classList.add("hidden");
        $("#btn-update").disabled = false;
        $("#btn-signal").disabled = false;
        if (j.status === "error") alert("执行失败：" + j.message);
        await loadStatus();
        await loadSignal();
      }
    } catch { /* ignore */ }
  }, 1200);
}

async function run(kind) {
  try {
    const r = await api(`/api/${kind}`, { method: "POST", body: "{}" });
    if (r.started === false) alert("已有任务在运行");
    startPolling();
  } catch (e) { alert(e.message); }
}

/* ---------------- 选股信号 ---------------- */
async function loadSignal() {
  const sig = await api("/api/signal");
  if (sig.empty) return;
  STATE.signal = sig;
  renderSignal(sig);
}

function renderSignal(sig) {
  const capHint = sig.capital_source === "holdings"
    ? `现金 ${money(sig.cash)}`
    : "来自参数设置";
  const cards = [
    ["信号日", sig.date, ""],
    ["可选标的", nf(sig.n_eligible), "只"],
    ["选出", nf(sig.n_picks), "只"],
    ["总资产", money(sig.capital), capHint],
    ["持仓市值", money(sig.market_value ?? 0), "按信号日收盘"],
    ["行业分散", Object.keys(sig.industry_count || {}).length, "个"],
  ];
  $("#sig-cards").innerHTML = cards.map(([k, v, u], i) =>
    `<div class="card ${i === 2 ? "accent" : ""}"><div class="k">${k}</div>
     <div class="v">${v}<small>${u}</small></div></div>`).join("");

  const showF = STATE.factorCols;
  const fkeys = ["ep", "bp", "momentum", "lowvol", "turnover", "size", "reversal", "ocf_ni", "sue", "fcst_up", "roe", "gm"];
  const head = `<tr><th>#</th><th>代码</th><th>名称</th><th>行业</th>
    <th class="num">权重</th><th class="num">综合分</th><th>新闻</th>
    <th class="num">参考价</th>
    <th class="num">买入参考</th><th class="num">目标价</th>
    <th class="num">止盈 / 止损</th>
    <th class="num">建议股数</th><th class="num">金额</th>
    ${showF ? fkeys.map(k => `<th class="num">${F_LABEL[k]}</th>`).join("") : ""}</tr>`;
  $("#sig-table thead").innerHTML = head;

  const rows = (sig.picks || []).map((x, i) => {
    const f = x.factors || {};
    const fcells = showF ? fkeys.map(k => {
      const v = f[k];
      if (v === null || v === undefined) return `<td class="num">—</td>`;
      const t = (k === "momentum" || k === "reversal") ? (v * 100).toFixed(1) + "%" : Number(v).toFixed(3);
      return `<td class="num mono">${t}</td>`;
    }).join("") : "";
    const nw = x.news;
    const ncls = !nw ? "neu" : nw.label === "利好" ? "pos" : nw.label === "利空" ? "neg" : "neu";
    const nz = nw ? (Math.abs(nw.z) < 0.005 ? 0 : nw.z) : 0;
    const ncell = nw
      ? `<span class="badge ${ncls}">${nw.label}</span> <span class="mono" style="color:#6b7480">${nz > 0 ? "+" : ""}${nz.toFixed(2)}</span>`
      : `<span class="muted">—</span>`;
    const ref = x.pricing || {};
    const refCell = (ref.buy_limit || ref.buy_cap)
      ? `<span class="mono">${ref.buy_limit ?? "—"}</span> <span style="color:#9aa2ad">/</span> <span class="mono" style="color:#6b7480">${ref.buy_cap ?? "—"}</span>`
      : `<span class="muted">—</span>`;
    const tgtCell = ref.target
      ? `<span class="mono">${ref.target}</span> <span style="color:#6b7480;font-size:11px">${ref.exp_ret >= 0 ? "+" : ""}${(ref.exp_ret * 100).toFixed(1)}%</span>`
      : `<span class="muted">—</span>`;
    const tpslCell = (ref.take_profit || ref.stop)
      ? `<span class="mono" style="color:#1a9c62">${ref.take_profit ?? "—"}</span><span style="color:#9aa2ad"> / </span><span class="mono" style="color:#c0392b">${ref.stop ?? "—"}</span>`
      : `<span class="muted">—</span>`;
    return `<tr>
      <td class="mono">${i + 1}</td>
      <td class="mono">${x.code}</td>
      <td>${x.name}</td>
      <td style="color:#6b7480">${x.industry}</td>
      <td class="num">${pct(x.weight, 2)}</td>
      <td class="num mono">${x.score ? x.score.toFixed(3) : "—"}</td>
      <td>${ncell}</td>
      <td class="num mono">${x.price ?? "—"}</td>
      <td class="num">${refCell}</td>
      <td class="num">${tgtCell}</td>
      <td class="num">${tpslCell}</td>
      <td class="num mono">${nf(x.shares)}</td>
      <td class="num mono">${nf(x.amount)}</td>
      ${fcells}</tr>`;
  }).join("");
  $("#sig-table tbody").innerHTML = rows ||
    `<tr><td colspan="13" class="empty">没有选股结果</td></tr>`;
}

/* ---------------- 新闻情绪 ---------------- */
let liveTimer = null;

async function loadNews() {
  try {
    const d = await api("/api/news");
    renderNews(d);
  } catch (e) {
    $("#news-table tbody").innerHTML =
      `<tr><td colspan="7" class="empty">读取失败：${e.message}</td></tr>`;
  }
  await loadLive();
}

async function loadLive() {
  try { renderLive(await api("/api/news/live")); } catch { }
}

function renderLive(d) {
  // 页面跑的是旧版前端时自动刷新，避免"看起来没更新"
  if (d && d.v && d.v !== APP_V) { location.reload(); return; }
  if (!d || d.updated === null) {
    $("#news-time").textContent = "实时流启动中…";
    return;
  }
  const idx = d.index ?? 0;
  $("#gauge-fill").style.left = `${(idx + 1) / 2 * 100}%`;
  $("#gauge-val").textContent = `${idx > 0 ? "+" : ""}${idx.toFixed(2)}`;
  $("#gauge-val").style.color = idx > 0.15 ? "#1a9c62" : idx < -0.15 ? "#c0392b" : "#1a1d24";
  const nsrc = Object.keys(d.sources || {}).length;
  $("#news-time").innerHTML =
    `<span class="live-dot"></span>实时 · 拉取 ${d.updated}` +
    (d.newest ? ` · 源站最新 ${d.newest.slice(11)}` : "") +
    (nsrc ? ` · ${nsrc} 源` : "");
  $("#market-stats").innerHTML = `
    <div>利好条数<b>${d.pos ?? 0}</b></div>
    <div>利空条数<b>${d.neg ?? 0}</b></div>
    <div>3日样本<b>${d.n ?? 0}</b></div>
    <div>持仓命中<b>${(d.items || []).filter(x => x.hit).length}</b></div>`;

  $("#feed-table tbody").innerHTML = (d.items || []).map(h => {
    const c = h.score > 0.3 ? "#1a9c62" : h.score < -0.3 ? "#c0392b" : "#6b7480";
    const hit = h.hit ? `<span class="badge pos" style="margin-right:6px">持仓</span>` : "";
    const hard = h.hard ? `<span class="badge neg" style="margin-right:6px">硬利空</span>` : "";
    const src = h.src ? `<span class="src">${h.src}</span>` : "";
    return `<tr><td class="mono" style="color:#6b7480">${h.time.slice(11)}</td>
      <td>${src}${hit}${hard}<span class="ntitle">${h.title}</span></td>
      <td class="num mono" style="color:${c}">${h.score > 0 ? "+" : ""}${h.score.toFixed(2)}</td></tr>`;
  }).join("") || `<tr><td colspan="3" class="empty">暂无快讯</td></tr>`;
}

function renderNews(d) {
  const mk = d.market || {};
  $("#news-time").textContent = d.built ? `更新于 ${d.built}` : "";
  $("#market-stats").innerHTML = `
    <div>利好条数<b>${mk.pos ?? 0}</b></div>
    <div>利空条数<b>${mk.neg ?? 0}</b></div>
    <div>样本<b>${mk.n ?? 0}</b></div>
    <div>候选股<b>${(d.stocks || []).length}</b></div>`;
  const hs = mk.headlines || [];
  if (hs.length) {
    $("#feed-table tbody").innerHTML = hs.map(h => {
      const c = h.score > 0.3 ? "#1a9c62" : h.score < -0.3 ? "#c0392b" : "#6b7480";
      return `<tr><td class="mono" style="color:#6b7480">${h.time.slice(11)}</td>
        <td><span class="ntitle">${h.title}</span></td>
        <td class="num mono" style="color:${c}">${h.score > 0 ? "+" : ""}${h.score.toFixed(2)}</td></tr>`;
    }).join("");
  }

  const st = (d.stocks || []).filter(s => s.n > 0)
    .sort((a, b) => Math.abs(b.z) - Math.abs(a.z)).slice(0, 60);
  $("#news-table tbody").innerHTML = st.map(s => {
    const cls = s.label === "利好" ? "pos" : s.label === "利空" ? "neg" : "neu";
    const t = (s.items && s.items[0]) ? s.items[0].title : "";
    return `<tr><td class="mono">${s.code}</td><td>${s.name || ""}</td>
      <td class="num mono">${s.score.toFixed(2)}</td>
      <td class="num mono">${s.z > 0 ? "+" : ""}${s.z.toFixed(2)}</td>
      <td><span class="badge ${cls}">${s.label}</span></td>
      <td class="num">${s.n}</td>
      <td><span class="ntitle">${t}</span></td></tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">还没有新闻数据</td></tr>`;
}

function startLive() {
  stopLive();
  loadLive();
  liveTimer = setInterval(loadLive, 10000);
}
function stopLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}

/* ---------------- 调仓单 ---------------- */
async function loadHoldings() {
  const h = await api("/api/holdings");
  $("#h-cash").value = h.cash || 0;
  $("#h-pos").value = Object.entries(h.positions || {})
    .map(([c, s]) => `${c},${Math.round(s)}`).join("\n");
}

function parseHoldings() {
  const pos = {};
  $("#h-pos").value.split(/\n+/).forEach((line) => {
    const m = line.match(/(\d{6})\D+(\d+)/);
    if (m) pos[m[1]] = Number(m[2]);
  });
  return { cash: Number($("#h-cash").value || 0), positions: pos };
}

async function doOrders(silent = false) {
  try {
    const h = parseHoldings();
    await api("/api/holdings", { method: "POST", body: JSON.stringify(h) });
    const res = await api("/api/orders");
    renderOrders(res, silent);
  } catch (e) { if (!silent) alert(e.message); }
}

function renderOrders(res, silent = false) {
  STATE.orders = res;
  const cards = [
    ["总资产", money(res.total_assets), ""],
    ["持仓市值", money(res.market_value), ""],
    ["可用现金", money(res.cash), ""],
    ["买入", `${res.n_buy} 笔`, money(res.buy_amount)],
    ["卖出", `${res.n_sell} 笔`, money(res.sell_amount)],
    ["预估费用", money(res.est_cost), "含佣金+印花税+滑点"],
  ];
  $("#ord-cards").innerHTML = cards.map(([k, v, u], i) =>
    `<div class="card ${i === 3 ? "accent" : ""}"><div class="k">${k}</div>
     <div class="v">${v}<small>${u}</small></div></div>`).join("");

  const cls = { BUY: "buy", SELL: "sell", HOLD: "hold" };
  const cn = { BUY: "买入", SELL: "卖出", HOLD: "不动" };
  $("#ord-table tbody").innerHTML = (res.orders || []).map((r, i) => {
    const rec = r.action === "HOLD" || !r.price
      ? `<span style="color:#c3c8d0">—</span>`
      : `<button class="btn tiny plus" data-i="${i}" title="记入成交记录">+</button>`;
    return `<tr>
    <td><span class="badge ${cls[r.action]}">${cn[r.action]}</span></td>
    <td class="mono">${r.code}</td><td>${r.name || ""}</td>
    <td class="num mono">${r.price ?? "—"}</td>
    <td class="num">${refCell(r)}</td>
    <td class="num mono">${nf(r.current_shares)}</td>
    <td class="num mono">${nf(r.target_shares)}</td>
    <td class="num mono">${r.delta > 0 ? "+" : ""}${nf(r.delta)}</td>
    <td class="num mono">${nf(r.amount)}</td>
    <td class="num mono">${nf(r.fee)}</td>
    <td>${rec}</td></tr>`;
  }).join("") || `<tr><td colspan="11" class="empty">没有需要调整的持仓</td></tr>`;

  $$("#ord-table .plus").forEach(b => {
    b.onclick = () => recordOrder(Number(b.dataset.i));
  });
  if (res.cash_scale < 0.999 && !silent) {
    alert(`现金不足，买入已按 ${(res.cash_scale * 100).toFixed(0)}% 缩减`);
  }
}

/* 调仓单的「参考成交价」单元格：买入=挂单/可追，卖出=挂单/最低接受 */
function refCell(r) {
  if (r.post_price == null && r.edge_price == null) return `<span class="muted">—</span>`;
  const head = r.action === "BUY" ? "挂" : "挂";
  const tail = r.action === "BUY" ? "追" : "底";
  return `<span class="mono">${head} ${r.post_price ?? "—"}</span>`
    + ` <span style="color:#9aa2ad">/</span> `
    + `<span class="mono" style="color:#6b7480">${tail} ${r.edge_price ?? "—"}</span>`;
}
/* ---------------- 成交记录 ---------------- */
function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2200);
}

async function recordOrder(i) {
  const res = STATE.orders;
  if (!res || !res.orders || !res.orders[i]) return;
  const r = res.orders[i];
  openTradeModal({
    code: r.code, name: r.name, side: r.action,
    shares: Math.abs(r.delta), price: r.price,
    tip: `调仓建议 ${Math.abs(r.delta)} 股，实际成交价可改`,
  });
}

/* ---- 记账弹窗 ---- */
function openTradeModal(p = {}) {
  STATE.tradeCtx = p;
  $("#tm-title").textContent = p.tip || "记一笔成交";
  $("#tm-code").value = p.code || "";
  $("#tm-name").value = p.name || "";
  $("#tm-side").value = p.side || "BUY";
  $("#tm-shares").value = p.shares || "";
  $("#tm-price").value = p.price || "";
  $("#tm-tip").textContent = "";
  updateTradeAmount();
  $("#trade-modal").classList.remove("hidden");
  setTimeout(() => $("#tm-shares").focus(), 50);
}

function closeTradeModal() {
  $("#trade-modal").classList.add("hidden");
}

function updateTradeAmount() {
  const sh = Number($("#tm-shares").value);
  const px = Number($("#tm-price").value);
  $("#tm-amount").textContent =
    (sh > 0 && px > 0) ? money(sh * px) : "—";
}

async function submitTrade() {
  const body = {
    code: $("#tm-code").value.trim(),
    name: $("#tm-name").value.trim(),
    side: $("#tm-side").value,
    shares: Number($("#tm-shares").value),
    price: Number($("#tm-price").value),
  };
  if (!/^\d{6}$/.test(body.code)) return alert("请输入 6 位股票代码");
  if (!(body.shares > 0)) return alert("股数必须大于 0");
  if (!(body.price > 0)) return alert("价格必须大于 0");
  try {
    await api("/api/trade", { method: "POST", body: JSON.stringify(body) });
    closeTradeModal();
    toast(`已记账：${body.code} ${cnSide(body.side)} ${body.shares} 股 @ ${body.price}`);
    await refreshOrdersView();
  } catch (e) {
    alert("记账失败：" + e.message);
  }
}

function cnSide(s) { return s === "BUY" ? "买入" : "卖出"; }

async function refreshOrdersView() {
  await loadHoldings();
  await doOrders(true);
  await loadTrades();
  await loadEquity();
}

async function recordTrade(body, label) {
  try {
    await api("/api/trade", { method: "POST", body: JSON.stringify(body) });
    toast("已记账：" + label);
    await refreshOrdersView();
  } catch (e) {
    toast("记账失败：" + e.message, true);
  }
}

async function recordAll() {
  const res = STATE.orders;
  if (!res || !res.orders) return;
  const rows = res.orders.filter(r => r.action !== "HOLD" && r.price && r.delta);
  if (!rows.length) return toast("没有可记账的调仓", true);
  if (!confirm(`将把 ${rows.length} 笔调仓全部记入成交记录，确定？`)) return;
  let ok = 0, fail = 0;
  for (const r of rows) {
    try {
      await api("/api/trade", {
        method: "POST", body: JSON.stringify({
          code: r.code, name: r.name, side: r.action,
          shares: Math.abs(r.delta), price: r.price,
        })
      });
      ok++;
    } catch { fail++; }
  }
  toast(`已记账 ${ok} 笔${fail ? `，失败 ${fail} 笔` : ""}`, fail > 0);
  await refreshOrdersView();
}

/* ---- 每日资产 ---- */
async function loadEquity() {
  try {
    const d = await api("/api/equity");
    renderEquity(d);
  } catch { }
}

function renderEquity(d) {
  const s = d.summary || {};
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const sign = (v) => (v > 0 ? "+" : "") + money(v);
  const cards = [
    ["总资产", money(s.total || 0), `现金 ${money(s.cash || 0)}`],
    ["今日盈亏", sign(s.today_pnl || 0), pct(s.today_pct || 0, 2)],
    ["累计盈亏", sign(s.total_pnl || 0), pct(s.total_pct || 0, 2)],
    ["持仓市值", money(s.market_value || 0), `记录 ${s.days || 0} 天`],
  ];
  $("#eq-cards").innerHTML = cards.map(([k, v, u], i) => {
    const color = (i === 1) ? cls(s.today_pnl) : (i === 2 ? cls(s.total_pnl) : "");
    return `<div class="card ${i === 0 ? "accent" : ""}"><div class="k">${k}</div>
      <div class="v ${color}" style="font-size:${i ? 20 : 22}px">${v}</div>
      <div class="k" style="margin:2px 0 0">${u}</div></div>`;
  }).join("");

  const rows = (d.rows || []).slice().reverse();
  let prev = null;
  const out = rows.map((r, idx) => {
    const older = rows[idx + 1];
    const pnl = older ? r.total - older.total : 0;
    const p = older && older.total ? pnl / older.total : 0;
    return `<tr>
      <td class="mono">${r.date}</td>
      <td class="num mono">${nf(r.total)}</td>
      <td class="num mono">${nf(r.market_value)}</td>
      <td class="num mono">${nf(r.cash)}</td>
      <td class="num mono ${pnl > 0 ? "pos" : pnl < 0 ? "neg" : ""}">${older ? (pnl > 0 ? "+" : "") + nf(pnl) : "—"}</td>
      <td class="num mono ${p > 0 ? "pos" : p < 0 ? "neg" : ""}">${older ? pct(p, 2) : "—"}</td>
    </tr>`;
  }).join("");
  $("#eq-table tbody").innerHTML = out ||
    `<tr><td colspan="6" class="empty">还没有资产记录</td></tr>`;
}

async function loadTrades() {
  try {
    const d = await api("/api/trades");
    renderTrades(d.trades || []);
  } catch { }
}

function renderTrades(rows) {
  const buy = rows.filter(r => r.side === "BUY").length;
  const sell = rows.filter(r => r.side === "SELL").length;
  $("#trade-summary").textContent =
    rows.length ? `共 ${rows.length} 笔（买入 ${buy} / 卖出 ${sell}）` : "";
  const cls = { BUY: "buy", SELL: "sell" };
  const cn = { BUY: "买入", SELL: "卖出" };
  $("#trade-table tbody").innerHTML = rows.map(r => `<tr>
    <td class="mono" style="color:#6b7480">${r.time.slice(5, 16)}</td>
    <td><span class="badge ${cls[r.side]}">${cn[r.side]}</span></td>
    <td class="mono">${r.code}</td><td>${r.name || ""}</td>
    <td class="num mono">${nf(r.shares)}</td>
    <td class="num mono">${r.price}</td>
    <td class="num mono">${nf(r.amount)}</td>
    <td class="num mono">${nf(r.fee)}</td>
    <td><button class="btn tiny ghost" data-id="${r.id}">撤销</button></td>
  </tr>`).join("") || `<tr><td colspan="9" class="empty">还没有成交记录</td></tr>`;

  $$("#trade-table button[data-id]").forEach(b => {
    b.onclick = async () => {
      if (!confirm("撤销这笔记录？持仓和现金会回退。")) return;
      try {
        await api("/api/trade/undo", {
          method: "POST", body: JSON.stringify({ id: b.dataset.id })
        });
        toast("已撤销");
        await loadHoldings();
        await doOrders(true);
        await loadTrades();
      } catch (e) { toast("撤销失败：" + e.message, true); }
    };
  });
}

/* ---------------- 回测表现 ---------------- */
async function loadPerf() {
  if (STATE.perf) return renderPerf(STATE.perf);
  const p = await api("/api/performance");
  STATE.perf = p;
  renderPerf(p);
}

function renderPerf(p) {
  if (!p.available) {
    $("#perf-cards").innerHTML = `<div class="card"><div class="k">回测</div><div class="v">—</div></div>`;
    return;
  }
  const cards = [
    ["年化收益", pct(p.cagr), `基准 ${pct(p.bench_cagr)}`],
    ["年化超额", pct(p.ann_excess), `IR ${p.ir}`],
    ["夏普比率", p.sharpe, `波动 ${pct(p.vol)}`],
    ["最大回撤", pct(p.mdd), `卡玛 ${p.calmar}`],
    ["月胜率", pct(p.win_rate_month), `${p.years} 年`],
    ["年化换手", pct(p.ann_turnover, 0), "单边"],
  ];
  $("#perf-cards").innerHTML = cards.map(([k, v, u], i) =>
    `<div class="card ${i === 1 ? "accent" : ""}"><div class="k">${k}</div>
     <div class="v">${v}<small>${u}</small></div></div>`).join("");
  $("#perf-range").textContent = `${p.start} ~ ${p.end}`;
  $("#img-nav").src = "/img/fig_performance.png?t=" + Date.now();
  $("#img-ic").src = "/img/fig_ic.png?t=" + Date.now();

  const y = p.yearly || {};
  $("#year-table tbody").innerHTML = Object.keys(y).sort().map(k => {
    const r = y[k], ex = r["超额"];
    return `<tr><td>${k}</td>
      <td class="num ${r["策略"] >= 0 ? "" : "neg"}">${pct(r["策略"])}</td>
      <td class="num">${pct(r["基准"])}</td>
      <td class="num" style="color:${ex >= 0 ? "#1a9c62" : "#c0392b"}">${ex >= 0 ? "+" : ""}${pct(ex)}</td></tr>`;
  }).join("");
  renderPerfDocs(p);
}

/* 指标说明：上面那几个数字分别是什么、怎么读 */
const PERF_DOC = [
  ["年化收益", "(期末净值)^(1/年数) − 1", "按复利折算成“平均每年赚多少”。7.8 年翻 3 倍 ≈ 年化 19.5%。"],
  ["基准年化", "中证800 同期年化", "不选股、直接买指数躺平的收益，用来判断策略到底有没有本事。"],
  ["年化超额", "策略年化 − 基准年化", "扣掉大盘涨跌之后多赚的部分，是选股能力的直接体现。"],
  ["波动率", "日收益标准差 × √252", "净值上下晃的幅度。20% 出头属于中高波动，和偏股基金差不多。"],
  ["夏普比率", "(年化收益 − 无风险利率) ÷ 波动率", "每承担 1 单位波动换来多少收益。>1 优秀，0.5~1 一般，<0.3 偏弱。"],
  ["最大回撤", "净值 ÷ 历史最高净值 − 1 的最小值", "从最高点跌下来最惨的一次。−33% 表示曾经浮亏三分之一，得扛得住。"],
  ["卡玛比率", "年化收益 ÷ |最大回撤|", "每承受 1% 回撤换多少年化。0.6 = 1% 回撤换 0.6% 年化。"],
  ["信息比率 IR", "年化超额 ÷ 超额收益的波动", "超额稳不稳。>0.8 说明超额不是撞运气，<0.3 说明时灵时不灵。"],
  ["贝塔 beta", "策略日收益对基准日收益回归的斜率", "和大盘同涨同跌的程度。0.93 ≈ 大盘涨 1%，策略平均涨 0.93%。"],
  ["月胜率", "月度收益 > 0 的月份占比", "赚钱月份的比例。58% 属正常——胜率不用高，输小赢大就够。"],
  ["超额胜率", "月度跑赢基准的月份占比", "战胜指数的频率，衡量相对强弱。"],
  ["年化换手", "全年成交额 ÷ (2 × 平均市值)", "一年把仓位换几遍。6.2 ≈ 每年换 6 遍多；换手越高，交易成本吃得越多。"],
];

function renderPerfDocs(p) {
  const mb = $("#doc-metrics tbody");
  if (mb) {
    mb.innerHTML = PERF_DOC.map(([k, f, how]) =>
      `<tr><td><b>${k}</b></td>
        <td class="mono" style="font-size:12px;color:var(--muted)">${f}</td>
        <td>${how}</td></tr>`).join("");
  }
  const pb = $("#doc-params tbody");
  if (!pb) return;
  const c = p.cfg || {};
  const num = (v, d = 1) => (v === undefined || v === null || Number.isNaN(Number(v)))
    ? "—" : (Number(v) * 100).toFixed(d) + "%";
  const wt = Object.entries(c.weights || {})
    .map(([k, v]) => `${F_LABEL[k] || k} ${(Number(v) * 100).toFixed(1)}%`).join(" · ");
  const bp = (x) => (Number(x) * 1e4).toFixed(1) + "bp";
  const rows = [
    ["持仓只数 top_n", c.top_n ? c.top_n + " 只" : "—",
      "每期持有多少只。只数越少越集中，波动和最大回撤越大；回测里 50 只的夏普明显高于 30 只和 10 只。"],
    ["缓冲区倍数 buffer_ratio", c.buffer_ratio ? Number(c.buffer_ratio).toFixed(1) + "×" : "—",
      "老持仓只要还排在 top_n ~ top_n×该倍数 的名次内，就继续留着、不被新进者顶掉。用来压低换手和交易成本。"],
    ["因子权重", wt || "—",
      "综合分 = 各因子截面排序（行业内去均值、标准化）后按权重加权。权重 0 表示该因子不参与打分。"],
    ["单票上限 max_stock_w", num(c.max_stock_w, 0),
      "单只股票最多占多少仓位，防止押注过度集中。"],
    ["单行业上限 max_ind_w", num(c.max_ind_w, 0),
      "同一行业最多占多少仓位，防止全仓压在一个板块上。"],
    ["成交额门槛 min_amt20", c.min_amt20 ? (c.min_amt20 / 1e8).toFixed(2) + " 亿元" : "—",
      "20 日平均成交额低于这个数直接剔除，保证买得进、卖得出，也过滤掉被操纵的小票。"],
    ["最低股价 min_price", c.min_price ? Number(c.min_price).toFixed(2) + " 元" : "—",
      "太便宜的股票（接近 1 元）容易退市、流动性差，直接排除。"],
    ["上市天数 min_list_days", c.min_list_days ? c.min_list_days + " 个交易日" : "—",
      "次新股波动大、财报历史短，先排除在股票池外（250 交易日 ≈ 一年）。"],
    ["质量门槛 quality_screen", c.quality_screen ? "开启（ROE>0 且毛利率>0）" : "关闭",
      "把亏损或毛利为负的公司剔掉。实测当权重用反而降收益，所以只当门槛用。"],
    ["质量倾斜 quality_tilt", c.quality_tilt ? num(c.quality_tilt, 0) : "0（关闭）",
      "把 ROE/毛利率合成的质量分额外加到综合分上。实测越加收益越低，默认关闭。"],
    ["均线择时 risk_switch", c.risk_switch ? "开启" : "关闭",
      "中证800 跌破 200 日均线时把股票仓位降到设定比例。回测显示择时把年化砍掉一半，默认关闭。"],
    ["交易成本", (c.commission !== undefined)
      ? `佣金 ${bp(c.commission)} + 印花税 ${bp(c.stamp)} + 滑点 ${bp(c.slippage)}` : "—",
      (c.commission !== undefined)
        ? `单边合计约 ${bp(c.commission + c.stamp + c.slippage)}，已从净值里扣掉。真实成本可能更高（大单冲击）。`
        : "买卖都要付佣金、卖出还要付印花税，另外按滑点估一个价差损耗。"],
    ["无风险利率 rf", c.rf !== undefined ? num(c.rf) + "/年" : "—",
      "算夏普比率时的无风险收益，取 1.5% 约等于一年期国债。"],
    ["回测区间", c.start ? `${c.start} ~ ${c.end}` : "—",
      "历史回测覆盖的时间段。股票池只含现在还在上市的票，存在幸存者偏差，实盘按回测打 6~7 折。"],
  ];
  pb.innerHTML = rows.map(([k, v, how]) =>
    `<tr><td><b>${k}</b></td>
      <td class="num mono">${v}</td>
      <td>${how}</td></tr>`).join("");
}

/* ---------------- 设置 ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  STATE.settings = s;
  $("#s-topn").value = s.top_n;
  $("#s-ind").value = Math.round(s.max_ind_w * 100);
  $("#s-amt").value = Math.round(s.min_amt20 / 10000);
  $("#s-cap").value = s.capital;
  $("#s-qscreen").checked = !!s.quality_screen;
  $("#s-qtilt").value = Math.round((s.quality_tilt || 0) * 100);
  $("#s-qtilt-v").textContent = Math.round((s.quality_tilt || 0) * 100);
  $("#s-news").checked = !!s.news_enabled;
  $("#s-veto").checked = !!s.news_veto;
  $("#s-ntilt").value = Math.round((s.news_tilt || 0) * 100);
  $("#s-ntilt-v").textContent = Math.round((s.news_tilt || 0) * 100);
  $("#weights").innerHTML = Object.entries(s.weights).map(([k, v]) =>
    `<div class="wrow"><label>${F_LABEL[k] || k}</label>
      <input type="range" min="0" max="40" step="1" value="${Math.round(v * 100)}" data-k="${k}">
      <span class="val" id="w-${k}">${Math.round(v * 100)}%</span></div>`).join("");
  $$("#weights input[type=range]").forEach(el => {
    el.oninput = () => { $("#w-" + el.dataset.k).textContent = el.value + "%"; };
  });
  $("#s-qtilt").oninput = () => { $("#s-qtilt-v").textContent = $("#s-qtilt").value; };
  $("#s-ntilt").oninput = () => { $("#s-ntilt-v").textContent = $("#s-ntilt").value; };
}

async function saveSettings() {
  const S = STATE.settings || {};
  const num = (sel, fallback) => {
    const el = $(sel);
    if (!el || el.value === "" || el.value === null) return fallback;
    const v = Number(el.value);
    return Number.isFinite(v) ? v : fallback;
  };
  const w = {};
  $$("#weights input[type=range]").forEach(el => { w[el.dataset.k] = Number(el.value) / 100; });
  const body = {
    top_n: num("#s-topn", S.top_n ?? 50),
    max_ind_w: num("#s-ind", (S.max_ind_w ?? 0.25) * 100) / 100,
    min_amt20: num("#s-amt", (S.min_amt20 ?? 5e7) / 10000) * 10000,
    capital: num("#s-cap", S.capital ?? 1000000),
    weights: Object.keys(w).length ? w : (S.weights || {}),
    quality_screen: !!$("#s-qscreen").checked,
    quality_tilt: num("#s-qtilt", 0) / 100,
    news_enabled: !!$("#s-news").checked,
    news_veto: !!$("#s-veto").checked,
    news_tilt: num("#s-ntilt", 15) / 100,
  };
  const saved = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
  STATE.settings = saved;
  alert(`已保存：持仓 ${saved.top_n} 只，资金 ¥${Number(saved.capital).toLocaleString()}。下次生成信号生效。`);
  await loadSettings();
}

/* ---------------- 导出 ---------------- */
function exportCSV() {
  const sig = STATE.signal;
  if (!sig) return alert("还没有信号");
  const fk = ["ep", "bp", "momentum", "lowvol", "turnover", "size", "reversal", "ocf_ni", "sue", "fcst_up", "roe", "gm"];
  const head = ["代码", "名称", "行业", "权重", "综合分", "新闻", "新闻z",
    "参考价", "买入挂单价", "可追上限", "卖出挂单价", "最低接受价", "目标价",
    "预计涨幅", "止损参考", "建议股数", "金额", ...fk];
  const lines = [head.join(",")];
  sig.picks.forEach(x => {
    const r = x.pricing || {};
    lines.push([x.code, x.name, x.industry, x.weight, x.score,
      x.news ? x.news.label : "", x.news ? x.news.z : "",
      x.price, r.buy_limit ?? "", r.buy_cap ?? "", r.sell_limit ?? "",
      r.sell_floor ?? "", r.target ?? "",
      r.exp_ret != null ? (r.exp_ret * 100).toFixed(2) + "%" : "",
      r.stop ?? "",
      x.shares, x.amount, ...fk.map(k => x.factors?.[k] ?? "")].join(","));
  });
  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `选股信号_${sig.date}.csv`;
  a.click();
}

/* ---------------- 事件绑定 ---------------- */
$$(".tab").forEach(t => t.onclick = async () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === t));
  ["signal", "news", "orders", "3d", "perf", "settings"].forEach(k =>
    $("#tab-" + k).hidden = (k !== t.dataset.tab));
  if (t.dataset.tab === "3d") {
    const f = $("#frame3d");
    if (!f.src) f.src = "/static/3d.html";
    else f.contentWindow.location.reload();
  }
  if (t.dataset.tab === "news") { loadNews(); loadLive(); }
  else stopLive();
  if (t.dataset.tab === "orders") {
    await loadHoldings();
    await loadTrades();
    await loadEquity();
    const h = parseHoldings();
    if (Object.keys(h.positions).length || h.cash > 0) doOrders(true);
  }
  if (t.dataset.tab === "perf") loadPerf();
  if (t.dataset.tab === "settings") { loadSettings(); loadLog(); }
});

$("#btn-update").onclick = () => run("update");
$("#btn-signal").onclick = () => run("signal");
$("#btn-orders").onclick = doOrders;
$("#btn-csv").onclick = exportCSV;
$("#btn-save").onclick = saveSettings;
$("#btn-reset").onclick = async () => {
  await api("/api/settings", { method: "POST", body: "{}" });
  await loadSettings();
};
$("#btn-clear").onclick = () => { $("#h-pos").value = ""; $("#h-cash").value = 0; };
$("#btn-record-all").onclick = recordAll;
$("#btn-manual-trade").onclick = () => openTradeModal({ side: "BUY" });
$("#tm-ok").onclick = submitTrade;
$("#tm-cancel").onclick = closeTradeModal;
$("#tm-close").onclick = closeTradeModal;
$("#tm-shares").oninput = updateTradeAmount;
$("#tm-price").oninput = updateTradeAmount;
$("#trade-modal").addEventListener("click", (e) => {
  if (e.target.id === "trade-modal") closeTradeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeTradeModal();
});
$("#btn-from-signal").onclick = async () => {
  try {
    await api("/api/holdings/from_signal", { method: "POST", body: "{}" });
    await loadHoldings();
    alert("已按当前信号填入演示持仓（不含现金）");
  } catch (e) { alert(e.message); }
};
$("#show-factors").onchange = (e) => {
  STATE.factorCols = e.target.checked;
  if (STATE.signal) renderSignal(STATE.signal);
};
async function loadLog() {
  try {
    const r = await fetch("/api/log");
    $("#log-box").textContent = (await r.text()) || "（空）";
  } catch { }
}
$("#btn-log").onclick = loadLog;

/* ---------------- 启动 ---------------- */
function openTab(name) {
  const t = $$(".tab").find(x => x.dataset.tab === name);
  if (t) t.click();
  window.scrollTo(0, 0);
}

(async function init() {
  await loadStatus();
  await loadSignal();
  loadLive();
  setInterval(loadLive, 10000);          // 全局实时快讯轮询（10 秒）
  setInterval(loadStatus, 60000);
  // 心跳：桌面模式下关掉窗口后，启动器靠它自动退出
  const ping = () => fetch("/api/ping").catch(() => { });
  ping();
  setInterval(ping, 5000);
  const hash = location.hash.replace("#", "");
  if (["signal", "news", "orders", "3d", "perf", "settings"].includes(hash)) openTab(hash);
  window.addEventListener("hashchange", () => openTab(location.hash.replace("#", "")));
})();
