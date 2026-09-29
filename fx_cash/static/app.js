// 外币现钞平台前端（原生 JS）
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const state = { teller: "T01", role: "cashier", inv: null, month: null };

const STATUS_CN = {
  draft: "草稿", held: "已锁库", confirmed: "已确认",
  replacement_offered: "替换待确认", replacement_confirmed: "替换已确认",
  fulfilled: "已交付", cancelled: "已取消", expired: "已过期",
};
const fmtCny = (x) => (x == null ? "—" : Number(x).toFixed(2));
const fmtFx = (x) => (x == null ? "—" : Number(x).toFixed(4));
const fmtRate = (x) => (x == null ? "—" : Number(x).toFixed(x >= 1 ? 4 : 5));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3600);
}

async function withTeller() {
  const ts = await api("/api/tellers");
  const sel = $("#tellerSelect");
  sel.innerHTML = ts.map((t) =>
    `<option value="${t.id}">${t.id} · ${esc(t.name)}（${t.role === "supervisor" ? "主管" : "柜员"}）</option>`).join("");
  sel.value = state.teller;
  const apply = () => {
    const t = ts.find((x) => x.id === sel.value);
    state.teller = t.id; state.role = t.role;
    $("#tellerRole").textContent = t.role === "supervisor" ? "主管" : "柜员";
    render(currentView);
  };
  sel.onchange = apply; apply();
}

// ---------------- 视图 1：币种库存矩阵 + 新建预约 ----------------
async function viewInventory() {
  const branch = state.branch || "B01";
  state.inv = await api(`/api/inventory?branch=${branch}`);
  const d = state.inv;
  const byCur = {};
  d.denominations.forEach((x) => (byCur[x.currency] ||= []).push(x));

  const matrixRows = d.currencies.map((c) => {
    const cells = byCur[c.code].map((dn) => {
      const s = d.stock[dn.id] || { total: 0, locked: 0, free: 0 };
      const cls = s.free === 0 ? "cell-zero" : s.free <= 5 ? "cell-tight" : "";
      return `<td class="${cls}"><span class="cell-free">${s.free}</span>
        <span class="cell-meta">锁 ${s.locked} / 共 ${s.total}</span></td>`;
    }).join("");
    return `<tr><td class="cur-cell">${c.code}<span class="cell-meta">${esc(c.name)}</span></td>${cells}</tr>`;
  }).join("");
  const maxCols = Math.max(...d.currencies.map((c) => byCur[c.code].length));

  $("#view").innerHTML = `
    <div class="card">
      <h2>网点币种 × 券别库存矩阵（张数）</h2>
      <div class="pill-row">
        ${d.branches.map((b) => `<button class="btn ${b.code === d.branch ? "btn-primary" : "btn-ghost"}"
          onclick="state.branch='${b.code}';render('inventory')">${b.code} · ${esc(b.name)}</button>`).join("")}
      </div>
      <div class="matrix-wrap">
        <table class="matrix tight">
          <thead><tr><th>币种 \\ 面额</th>${
            Array.from({ length: maxCols }, (_, i) => {
              const sample = d.currencies.map((c) => byCur[c.code][i]).filter(Boolean)[0];
              return `<th>${sample ? sample.face_value : ""}</th>`;
            }).join("")}</tr></thead>
          <tbody>${matrixRows}</tbody>
        </table>
      </div>
      <p class="muted" style="margin-top:8px">黄色：可用 ≤ 5 张；红色：无可用。单元格为 可用 / 锁定 / 总量。</p>
    </div>

    <div class="card">
      <h2>新建预约（保存草稿，提交时锁定库存）</h2>
      <div class="row">
        <div class="field"><label>客户（虚构）</label><input id="fName" value="林一帆" /></div>
        <div class="field"><label>证件号（虚构）</label><input id="fDoc" value="FX-330101-19900101-0001" /></div>
        <div class="field"><label>币种</label><select id="fCur">
          ${d.currencies.map((c) => `<option value="${c.code}">${c.code} ${esc(c.name)}</option>`).join("")}
        </select></div>
        <div class="field"><label>取钞有效期至</label><input id="fExp" type="datetime-local" value="2026-09-30T16:00" /></div>
      </div>
      <div id="fDenoms" style="margin:12px 0"></div>
      <button class="btn btn-lemon" onclick="createReservation()">保存预约</button>
    </div>`;

  const paintDenoms = () => {
    const cur = $("#fCur").value;
    $("#fDenoms").innerHTML = `<div class="row">${byCur[cur].map((dn) => `
      <div class="field"><label>${dn.face_value} ×</label>
        <input type="number" min="0" value="0" style="width:86px" data-denom="${dn.id}"></div>`).join("")}</div>`;
  };
  $("#fCur").onchange = paintDenoms; paintDenoms();
  state.branch = branch;
}

async function createReservation() {
  const lines = $$('#fDenoms input[data-denom]').map((i) =>
    ({ denom_id: Number(i.dataset.denom), qty: Number(i.value) })).filter((x) => x.qty > 0);
  try {
    const r = await api("/api/reservations", { method: "POST", body: {
      customer_name: $("#fName").value, id_doc: $("#fDoc").value,
      currency: $("#fCur").value, branch_code: state.branch, lines,
      expires_at: $("#fExp").value.replace("T", "T"), teller: state.teller,
    }});
    toast(`预约已保存：${r.id}（草稿，请到「柜面核验」提交锁库）`, "ok");
    render("counter");
  } catch (e) { toast(e.message, "err"); }
}

// ---------------- 视图 2：预约日历 ----------------
async function viewCalendar() {
  if (!state.month) state.month = "2026-09";
  const cal = await api(`/api/calendar?month=${state.month}`);
  const [y, m] = cal.month.split("-").map(Number);
  const shiftMonth = (delta) => {
    const nm = ((((m - 1 + delta) % 12) + 12) % 12) + 1;
    const ny = y + Math.floor((m - 1 + delta) / 12);
    state.month = `${ny}-${String(nm).padStart(2, "0")}`;
    render("calendar");
  };
  let cells = "";
  for (let i = 0; i < cal.first_weekday; i++) cells += `<div class="cal-cell empty"></div>`;
  for (let day = 1; day <= cal.days_in_month; day++) {
    const key = `${cal.month}-${String(day).padStart(2, "0")}`;
    const items = (cal.days[key] || []).map((it) =>
      `<div class="cal-item ${it.marker}" title="${esc(it.id)} ${STATUS_CN[it.status]}">
        ${it.marker === "created" ? "📝" : "⏰"} ${esc(it.customer_name)} · ${it.currency}
        ${it.cny_amount ? "¥" + fmtCny(it.cny_amount) : ""}</div>`).join("");
    cells += `<div class="cal-cell"><div class="cal-date">${day}</div>${items}</div>`;
  }
  $("#view").innerHTML = `
    <div class="card">
      <div class="cal-head">
        <button class="btn btn-ghost" onclick='shiftCal(-1)'>‹ 上月</button>
        <h2 style="margin:0">${cal.month} 预约日历</h2>
        <button class="btn btn-ghost" onclick='shiftCal(1)'>下月 ›</button>
        <span class="muted">蓝＝创建日，柠檬黄＝取钞期限</span>
      </div>
      <div class="cal-grid">
        ${["一","二","三","四","五","六","日"].map((w) => `<div class="cal-dow">周${w}</div>`).join("")}
        ${cells}
      </div>
    </div>`;
  window.shiftCal = shiftMonth;
}

// ---------------- 视图 3：汇率版本 ----------------
async function viewRates() {
  const rates = await api("/api/rates");
  const grouped = {};
  rates.forEach((r) => (grouped[r.currency] ||= []).push(r));
  const events = await api("/api/events?limit=50");
  $("#view").innerHTML = `
    <div class="card">
      <h2>汇率版本（1 外币兑本币 CNY）</h2>
      <table class="tight">
        <thead><tr><th>币种</th><th>版本#</th><th>生效日</th><th>发布时间</th>
        <th>汇率</th><th>说明</th></tr></thead>
        <tbody>${Object.entries(grouped).map(([cur, rs]) =>
          rs.map((r, i) => `<tr>
            <td class="num">${cur} ${r.symbol}${i === 0 ? ' <span class="flag-reviewed">最新</span>' : ""}</td>
            <td class="num">${r.id}</td><td>${r.eff_date}</td><td>${esc(r.published_at)}</td>
            <td class="num">${fmtRate(r.rate)}</td><td class="muted">${esc(r.note)}</td></tr>`).join("")
        ).join("")}</tbody>
      </table>
      <p class="muted" style="margin-top:8px">汇率仅在「确认预约」时生成快照写入预约；之后牌价变化不影响已确认金额，跨日确认记录事件。</p>
    </div>
    <div class="card"><h2>业务事件流水</h2><ul class="event-log">
      ${events.map((e) => `<li><span class="ts">${esc(e.ts)}</span><span class="et">${esc(e.type)}</span>
        ${e.reservation_id ? `[${esc(e.reservation_id)}] ` : ""}${esc(e.detail)}</li>`).join("")}
    </ul></div>`;
}

// ---------------- 视图 4：柜面核验（预约全生命周期操作） ----------------
async function viewCounter() {
  const list = await api("/api/reservations");
  const now = "2026-09-29T12:00:00";
  $("#view").innerHTML = `
    <div class="card">
      <h2>柜面核验 · 预约队列</h2>
      <div class="pill-row">
        <button class="btn btn-lemon" onclick="runExpiry()">扫描到期未到场并释放库存</button>
        <span class="muted">平台时钟演示基准：${now}（到期以 expires_at 判断）</span>
      </div>
      <div class="res-grid">${list.length ? list.map(resCard).join("") : "<p class='muted'>暂无预约</p>"}</div>
    </div>`;
}

function resCard(r) {
  const urgent = new Date(r.expires_at) < new Date("2026-09-30T12:00") &&
    ["held","confirmed","replacement_offered","replacement_confirmed"].includes(r.status);
  const dlCls = r.status === "expired" ? "dead" : urgent ? "urgent" : "";
  const acts = [];
  if (r.status === "draft") {
    acts.push(`<button class="btn btn-primary" onclick="act('${r.id}','submit')">提交并锁库</button>`);
  }
  if (r.status === "held") {
    acts.push(`<button class="btn btn-primary" onclick="act('${r.id}','confirm')">按当日汇率确认</button>`);
    acts.push(`<button class="btn btn-ghost" onclick="offerReplace('${r.id}')">提出券别替换</button>`);
  }
  if (r.status === "confirmed") {
    acts.push(`<button class="btn btn-ghost" onclick="offerReplace('${r.id}')">提出券别替换</button>`);
  }
  if (r.status === "replacement_offered") {
    acts.push(`<button class="btn btn-lemon" onclick="act('${r.id}','replacements/accept')">客户重新确认替换</button>`);
    acts.push(`<button class="btn btn-ghost" onclick="act('${r.id}','replacements/decline')">客户拒绝替换</button>`);
  }
  if (["held","confirmed","replacement_offered","replacement_confirmed"].includes(r.status)) {
    acts.push(`<button class="btn btn-ghost" onclick="cancelRes('${r.id}')">客户取消</button>`);
  }
  if (["confirmed","replacement_confirmed"].includes(r.status)) {
    acts.push(`<button class="btn btn-primary" onclick="render('delivery')">去交付 →</button>`);
  }
  return `<div class="res-card">
    <div class="res-head"><span class="rid">${esc(r.id)}</span>
      <span class="status s-${r.status}">${STATUS_CN[r.status]}</span></div>
    <div class="res-body">
      <div class="res-deadline ${dlCls}"><span>取钞期限</span><span>${esc(r.expires_at.replace("T"," "))}</span></div>
      <div><b>${esc(r.customer_name)}</b> <span class="muted">${esc(r.id_doc)}</span></div>
      <div class="res-lines">${r.branch_code} · ${esc(r.branch_name)} · ${r.currency}</div>
      <div class="res-lines">券别：${r.lines.map((l) =>
        `${l.face_value}×${l.qty}${l.kind === "replacement" ? "（替）" : ""}`).join("，")}</div>
      <div>汇率快照：<span class="num">${fmtRate(r.fx_rate)}</span>
        本币金额：<b class="num">¥ ${fmtCny(r.cny_amount)}</b></div>
      ${r.held.length ? `<div class="res-lines">已锁定：${r.held.map((h) => `券别#${h.denom_id}×${h.qty}`).join("，")}</div>` : ""}
      <div class="mini-actions">${acts.join("")}</div>
    </div>
  </div>`;
}

async function act(rid, action) {
  try {
    const r = await api(`/api/reservations/${rid}/${action}`, { method: "POST", body: { teller: state.teller }});
    toast(action.includes("confirm") && r.rate ? `已确认：汇率 ${fmtRate(r.rate)}，金额 ¥${fmtCny(r.cny_amount)}` : "操作成功", "ok");
    render("counter");
  } catch (e) { toast(e.message, "err"); }
}

async function cancelRes(rid) {
  const reason = prompt("取消原因", "客户行程变更");
  if (reason === null) return;
  try {
    await api(`/api/reservations/${rid}/cancel`, { method: "POST", body: { teller: state.teller, reason }});
    toast("已取消，锁定库存释放", "info"); render("counter");
  } catch (e) { toast(e.message, "err"); }
}

async function offerReplace(rid) {
  // 简化替换：对第一个预约券别，提示换成同币种另一券别
  const r = await api(`/api/reservations/${rid}`);
  const line = r.lines[0];
  const inv = await api(`/api/inventory?branch=${r.branch_code}`);
  const opts = inv.denominations.filter((d) => d.currency === r.currency)
    .map((d) => `${d.id}:${d.face_value}`).join("\n");
  const ans = prompt(
    `把原券别（行#${line.line_id}，面额 ${line.face_value} × ${line.qty} 张）替换为同币种另一面额。\n` +
    `可选项 denom_id:面额 =\n${opts}\n\n输入格式：新denom_id,新数量`, "");
  if (!ans) return;
  const [denomId, qty] = ans.split(",").map((x) => Number(x.trim()));
  if (!denomId || !qty) return toast("输入格式不正确", "err");
  try {
    await api(`/api/reservations/${rid}/replacements`, { method: "POST", body: {
      teller: state.teller, new_lines: [{ replace_line_id: line.line_id, denom_id: denomId, qty }],
    }});
    toast("替换方案已提出，等待客户重新确认", "info"); render("counter");
  } catch (e) { toast(e.message, "err"); }
}

async function runExpiry() {
  try {
    const r = await api(`/api/reservations/RV-X/expire`, { method: "POST", body: {}});
    toast(r.expired.length ? `已释放到期预约：${r.expired.join("，")}` : "无到期预约", "info");
    render("counter");
  } catch (e) { toast(e.message, "err"); }
}

// ---------------- 视图 5：现钞交付（并排对照 + 幂等 + 假钞/差额注入） ----------------
async function viewDelivery() {
  const list = await api("/api/reservations");
  const ready = list.filter((r) => ["confirmed", "replacement_confirmed"].includes(r.status));
  const dels = await api("/api/deliveries");
  $("#view").innerHTML = `
    <div class="card">
      <h2>现钞交付 · 模拟点验钞机</h2>
      ${ready.length ? `<div class="row">
        <div class="field"><label>选择预约</label><select id="dlPick">
          ${ready.map((r) => `<option value="${r.id}">${r.id} · ${esc(r.customer_name)} · ${r.currency}</option>`).join("")}
        </select></div>
        <div class="field"><label>幂等键（重复提交返回原记录）</label><input id="dlKey" value="KEY-DEMO-001" style="width:200px"></div>
      </div>
      <div id="dlOverrides" style="margin:12px 0"></div>
      <div class="row">
        <button class="btn btn-primary" onclick="doDeliver()">点验并交付</button>
        <button class="btn btn-ghost" onclick="doDeliver(true)">用相同幂等键再交一次（验证幂等）</button>
      </div>` : "<p class='muted'>没有待交付（已确认）的预约。</p>"}
    </div>
    <div class="card">
      <h2>交付明细：预约数量 / 实点数量 / 差额 并排对照</h2>
      <table class="tight">
        <thead><tr><th>预约</th><th>客户</th><th>面额</th><th>预约数量</th>
        <th>实点数量</th><th>差额</th><th>疑点</th><th>交付柜员</th><th>复核</th></tr></thead>
        <tbody>${dels.map((d) => `<tr>
          <td>${esc(d.reservation_id)}</td><td>${esc(d.customer_name)}</td>
          <td class="num">${d.currency} ${d.face_value}</td>
          <td class="num">${d.booked_qty}</td><td class="num">${d.counted_qty}</td>
          <td class="num ${d.diff_qty > 0 ? "diff-pos" : d.diff_qty < 0 ? "diff-neg" : "diff-zero"}">
            ${d.diff_qty > 0 ? "+" : ""}${d.diff_qty}</td>
          <td>${d.suspicious ? '<span class="flag-suspicious">假钞疑点</span>' : "—"}</td>
          <td>${esc(d.teller_name)}</td>
          <td>${d.reviewed_by ? `<span class="flag-reviewed">${esc(d.reviewer_name)}已复核</span>` : "待复核"}</td>
        </tr>`).join("") || "<tr><td colspan='9' class='muted center'>暂无交付记录</td></tr>"}</tbody>
      </table>
    </div>`;

  const paint = async () => {
    const rid = $("#dlPick")?.value;
    if (!rid) return;
    const r = await api(`/api/reservations/${rid}`);
    $("#dlOverrides").innerHTML = `<div class="row">${r.lines.map((l) => `
      <div class="field"><label>${l.face_value} 面额（预约 ${l.qty} 张）</label>
        <div class="row" style="gap:6px">
          <input type="number" id="ov-${l.denom_id}" value="${l.qty}" style="width:90px">
          <label class="row" style="flex-direction:row;align-items:center;gap:4px;font-size:12px">
            <input type="checkbox" id="sp-${l.denom_id}">假钞疑点</label></div></div>`).join("")}</div>
      <p class="muted">把实点数量改成与预约不同即可制造交付差额；勾选假钞疑点触发主管复核留痕。</p>`;
  };
  $("#dlPick") && ($("#dlPick").onchange = paint);
  if (ready.length) paint();
}

async function doDeliver(repeat) {
  const rid = $("#dlPick").value;
  const overrides = {};
  $$("#dlOverrides input[id^='ov-']").forEach((i) => {
    const id = Number(i.id.slice(3));
    overrides[id] = { counted: Number(i.value), suspicious: $("#sp-" + id).checked };
  });
  try {
    const r = await api(`/api/reservations/${rid}/deliver`, { method: "POST", body: {
      teller: state.teller, idempotency_key: $("#dlKey").value, overrides,
    }});
    toast(r.idempotent ? "幂等命中：返回原交付记录，库存未重复扣减" : "交付完成，现钞已出库",
          r.idempotent ? "info" : "ok");
    render("delivery");
  } catch (e) { toast(e.message, "err"); }
}

// ---------------- 视图 6：差额复核（主管权限 + 已复核不可改） ----------------
async function viewReview() {
  const dels = await api("/api/deliveries");
  const pend = dels.filter((d) => !d.reviewed_by && (d.diff_qty !== 0 || d.suspicious));
  const all = dels;
  $("#view").innerHTML = `
    <div class="kpi">
      <div class="box"><b>${state.role === "supervisor" ? "主管" : "柜员"}</b><span>当前操作员（${state.teller}）</span></div>
      <div class="box"><b>${pend.length}</b><span>待差额/疑点复核</span></div>
      <div class="box"><b>${all.filter((d) => d.reviewed_by).length}</b><span>已复核（金额锁定）</span></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>待复核清单（预约数量 / 实点数量 / 差额 并排）</h2>
      <table class="tight">
        <thead><tr><th>#</th><th>预约</th><th>面额</th><th>预约</th><th>实点</th>
        <th>差额</th><th>疑点</th><th>交付人</th><th>操作</th></tr></thead>
        <tbody>${pend.map((d) => `<tr>
          <td>${d.id}</td><td>${esc(d.reservation_id)}</td>
          <td class="num">${d.currency} ${d.face_value}</td>
          <td class="num">${d.booked_qty}</td><td class="num">${d.counted_qty}</td>
          <td class="num ${d.diff_qty ? "diff-neg" : "diff-zero"}">${d.diff_qty > 0 ? "+" : ""}${d.diff_qty}</td>
          <td>${d.suspicious ? '<span class="flag-suspicious">假钞疑点</span>' : "—"}</td>
          <td>${esc(d.teller_name)}</td>
          <td class="left">
            <button class="btn btn-lemon" onclick="review(${d.id})">主管复核通过</button>
            <button class="btn btn-ghost" onclick="amendTry(${d.id}, ${d.counted_qty + 1})">柜员尝试改金额</button>
          </td></tr>`).join("") || "<tr><td colspan='9' class='muted center'>无待复核项</td></tr>"}</tbody>
      </table>
    </div>
    <div class="card">
      <h2>已复核记录（柜员修改将被拒绝）</h2>
      <table class="tight">
        <thead><tr><th>#</th><th>预约</th><th>面额</th><th>预约</th><th>实点</th>
        <th>差额</th><th>复核人</th><th>意见</th><th>操作</th></tr></thead>
        <tbody>${all.filter((d) => d.reviewed_by).map((d) => `<tr>
          <td>${d.id}</td><td>${esc(d.reservation_id)}</td>
          <td class="num">${d.currency} ${d.face_value}</td>
          <td class="num">${d.booked_qty}</td><td class="num">${d.counted_qty}</td>
          <td class="num ${d.diff_qty ? "diff-neg" : "diff-zero"}">${d.diff_qty > 0 ? "+" : ""}${d.diff_qty}</td>
          <td>${esc(d.reviewer_name)}</td><td class="left">${esc(d.review_note)}</td>
          <td><button class="btn btn-ghost" onclick="amendTry(${d.id}, 999)">尝试修改</button></td>
        </tr>`).join("") || "<tr><td colspan='9' class='muted center'>暂无</td></tr>"}</tbody>
      </table>
      ${state.role !== "supervisor" ? '<p class="muted" style="margin-top:8px">当前为柜员身份，复核按钮会返回 403；请在右上角切换为主管 T03。</p>' : ""}
    </div>`;
}

async function review(id) {
  const note = prompt("复核意见", "账实复核无误，差额挂账处理");
  if (note === null) return;
  try {
    await api(`/api/deliveries/${id}/review`, { method: "POST", body: { supervisor: state.teller, note }});
    toast("复核完成，金额已锁定", "ok"); render("review");
  } catch (e) { toast(e.message, "err"); }
}

async function amendTry(id, n) {
  try {
    await api(`/api/deliveries/${id}/amend`, { method: "POST", body: { teller: state.teller, new_counted: n }});
    toast("复核前允许修正实点数量", "info"); render("review");
  } catch (e) { toast("已拒绝：" + e.message, "err"); }
}

// ---------------- 路由 ----------------
const VIEWS = {
  inventory: viewInventory, calendar: viewCalendar, rates: viewRates,
  counter: viewCounter, delivery: viewDelivery, review: viewReview,
};
let currentView = "inventory";
function render(view) {
  currentView = view;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  VIEWS[view]().catch((e) => { $("#view").innerHTML =
    `<div class="card"><h2>加载失败</h2><p class="diff-neg">${esc(e.message)}</p></div>`; });
}
$$("#tabs .tab").forEach((t) => (t.onclick = () => render(t.dataset.view)));
withTeller().then(() => render("inventory"));
