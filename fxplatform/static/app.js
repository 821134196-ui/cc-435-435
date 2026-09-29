/* 外币现钞平台前端（原生 JS，无依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

const STATUS_CN = {
  RESERVED: "已预约", CONFIRMED: "已确认", REPLACE_PENDING: "待客户重新确认",
  REVIEWED: "已交付待复核", DELIVERED: "已结清",
  CANCELLED: "客户取消", EXPIRED: "已过期",
};
const KIND_CN = {short: "短款", over: "多款", counterfeit: "假钞疑点", substitution: "券别替换"};

const state = {
  meta: null,
  ccy: "USD",
  detailCache: {},
};

// ---------------------------------------------------------------- api
async function api(path, body) {
  const opt = body === undefined
    ? {headers: {"X-Teller": currentTeller()}}
    : {method: "POST",
       headers: {"Content-Type": "application/json", "X-Teller": currentTeller()},
       body: JSON.stringify(body)};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({error: true, message: "响应解析失败"}));
  if (!res.ok || data.error) {
    const err = new Error(data.message || "请求失败");
    err.payload = data;
    throw err;
  }
  return data;
}

function flash(message, kind = "ok") {
  const box = $("flash");
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function currentTeller() { return $("currentTeller").value; }
function currentBranch() { return $("counterBranch").value; }
function isSupervisor() {
  const opt = [...$("currentTeller").selectedOptions][0];
  return opt?.dataset.role === "supervisor";
}
function fmt2(v) {
  if (v === null || v === undefined || v === "") return "-";
  return Number(v).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmt0(v) {
  if (v === null || v === undefined) return "-";
  return Number(v).toLocaleString("zh-CN");
}

// ---------------------------------------------------------------- modal
function openModal(html) {
  $("modalBody").innerHTML = html;
  $("modalMask").classList.add("show");
}
function closeModal() { $("modalMask").classList.remove("show"); }
$("modalMask").addEventListener("click", (e) => {
  if (e.target === $("modalMask")) closeModal();
});

// ---------------------------------------------------------------- tabs
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-page]");
  if (!btn) return;
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  btn.classList.add("active");
  $("page-" + btn.dataset.page).classList.add("active");
  App.PAGE_LOADERS[btn.dataset.page]?.();
});

const App = {
  PAGE_LOADERS: {},

  // ------------------------------------------------------------ 初始化
  async init() {
    state.meta = await api("/api/meta");
    this.fillTellers();
    this.fillBranches();
    this.fillCurrencyControls();
    document.getElementById("pubDate").value = new Date().toISOString().slice(0, 10);
    await this.loadInventory();
    await this.loadCalendar();
    await this.loadFx();
    await this.loadDiffs();
    // 每 60 秒轻刷日历与待复核计数
    setInterval(() => {
      if ($("page-calendar").classList.contains("active")) this.loadCalendar();
      this.loadDiffs();
    }, 60000);
  },

  fillTellers() {
    const sel = $("currentTeller");
    sel.innerHTML = state.meta.tellers.map((t) =>
      `<option value="${t.username}" data-role="${t.role}">${esc(t.name)}（${t.role === "supervisor" ? "主管" : "柜员"}）</option>`
    ).join("");
    sel.onchange = () => {
      $("tellerRole").textContent = isSupervisor() ? "主管：可发布汇率 / 差额复核 / 假钞处置" : "柜员：可预约 / 核验 / 交付";
      this.loadDiffs();
    };
    sel.onchange();
  },

  fillBranches() {
    const opts = state.meta.branches.map((b) =>
      `<option value="${b.code}">${esc(b.name)}（${b.code}）</option>`).join("");
    $("counterBranch").innerHTML = opts;
  },

  fillCurrencyControls() {
    $("matrixTabs").innerHTML = state.meta.currencies.map((c, i) =>
      `<button data-ccy="${c.code}" class="${i === 0 ? "active" : ""}">${c.code} ${esc(c.name)}</button>`
    ).join("");
    $("matrixTabs").onclick = (e) => {
      const b = e.target.closest("button[data-ccy]");
      if (!b) return;
      state.ccy = b.dataset.ccy;
      $("matrixTabs").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      this.renderMatrix();
    };
    $("pubCurrency").innerHTML = state.meta.currencies.map((c) =>
      `<option value="${c.code}">${c.code} ${esc(c.name)}</option>`).join("");
  },

  ccyMeta(code) { return state.meta.currencies.find((c) => c.code === code); },

  // ------------------------------------------------------------ 库存矩阵
  async loadInventory() {
    state.inventory = await api("/api/inventory");
    this.renderMatrix();
  },

  renderMatrix() {
    const data = state.inventory;
    const ccy = data.currencies.find((c) => c.code === state.ccy) || data.currencies[0];
    if (!ccy) { $("matrixWrap").textContent = "无数据"; return; }
    const head = `<tr><th>券别面值</th>${data.branches.map((b) =>
      `<th>${esc(b.name)}<br><span class="muted">${b.code}</span></th>`).join("")}</tr>`;
    const rows = ccy.denominations.map((d) => {
      const cells = data.branches.map((b) => {
        const inv = d[b.code] || {total: 0, locked: 0, available: 0};
        const low = inv.available <= 10 ? " low" : "";
        return `<td><span class="cell-avail${low}">${fmt0(inv.available)}</span>` +
          `<span class="cell-sub">总 ${fmt0(inv.total)} / <span class="locked">锁 ${fmt0(inv.locked)}</span></span></td>`;
      }).join("");
      return `<tr><td class="face">${ccy.code} ${fmt2(d.face_value)}</td>${cells}</tr>`;
    }).join("");
    $("matrixWrap").innerHTML =
      `<table class="matrix"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
  },

  // ------------------------------------------------------------ 日历
  async loadCalendar() {
    const cal = await api("/api/calendar");
    const days = Object.keys(cal.days).sort().reverse();
    if (!days.length) {
      $("calendarWrap").innerHTML = `<div class="card muted">暂无预约</div>`;
      return;
    }
    $("calendarWrap").innerHTML = days.map((day) => {
      const cards = cal.days[day].map((a) => this.apCard(a)).join("");
      return `<div class="day-col"><h4>${day} · ${cal.days[day].length} 笔</h4>${cards}</div>`;
    }).join("");
  },

  apCard(a) {
    const now = new Date();
    const dl = new Date(a.valid_until);
    const over = dl < now;
    const items = a.items.map((it) =>
      `<li>${a.currency_code} ${fmt2(it.face_value)} × ${fmt0(it.planned_qty)}</li>`
    ).join("");
    const actions = this.apActions(a);
    return `<div class="ap-card status-${a.status} ${a.expiring_soon ? "expiring" : ""}">
      <div><span class="ap-code">${a.code}</span>
        <span class="badge ${a.status}">${STATUS_CN[a.status] || a.status}</span></div>
      <div class="ap-cust">${esc(a.customer_name)} · ${esc(a.id_type)} ${esc(a.id_no)}</div>
      <div class="ap-cust">${esc(a.branch_name)}（${a.branch_code}）</div>
      <ul class="items">${items}</ul>
      <div class="deadline ${over ? "over" : ""}">取钞截止 ${a.valid_until.replace("T", " ")}</div>
      ${a.amount_cny ? `<div class="ap-cust mono">折人民币 ${fmt2(a.amount_cny)} 元（外币 ${fmt2(a.amount_foreign)}）</div>` : ""}
      <div class="row-actions">${actions}</div>
    </div>`;
  },

  apActions(a) {
    const c = a.code;
    const btns = [];
    if (a.status === "RESERVED") {
      btns.push(`<button class="btn small lemon" onclick="App.confirmAp('${c}')">客户确认</button>`);
      btns.push(`<button class="btn small danger" onclick="App.cancelAp('${c}')">取消</button>`);
    }
    if (a.status === "CONFIRMED") {
      btns.push(`<button class="btn small" onclick="App.gotoDeliver('${c}')">去交付</button>`);
      btns.push(`<button class="btn small ghost" onclick="App.openReplacement('${c}')">券别替换</button>`);
      btns.push(`<button class="btn small danger" onclick="App.cancelAp('${c}')">取消</button>`);
    }
    if (a.status === "REPLACE_PENDING") {
      btns.push(`<button class="btn small lemon" onclick="App.decideReplace('${c}',true)">客户接受新方案</button>`);
      btns.push(`<button class="btn small ghost" onclick="App.decideReplace('${c}',false)">拒绝（维持原券别）</button>`);
    }
    if (a.status === "REVIEWED") {
      btns.push(`<button class="btn small" onclick="App.gotoReview()">去差额复核</button>`);
    }
    btns.push(`<button class="btn small ghost" onclick="App.viewDetail('${c}')">详情</button>`);
    return btns.join("");
  },

  // ------------------------------------------------------------ 新建预约
  openCreate() {
    const denoms = (code) => this.ccyMeta(code).denominations.map((d) =>
      `<label>${code} ${fmt2(d.face_value)}
        <input type="number" min="0" step="1" data-did="${d.id}" value="0" class="denom-qty"></label>`
    ).join("");
    openModal(`
      <h3>新建外币现钞预约（虚构客户信息）</h3>
      <div class="form-row">
        <label>客户姓名<input id="fName" value="林一舟"></label>
        <label>证件类型
          <select id="fIdType"><option>护照</option><option>港澳通行证</option><option>身份证</option></select></label>
        <label>证件号码<input id="fIdNo" value="E12345678"></label>
        <label>联系电话<input id="fPhone" value="138-0000-0001"></label>
      </div>
      <div class="form-row">
        <label>币种
          <select id="fCcy">${state.meta.currencies.map((c) =>
            `<option value="${c.code}">${c.code} ${esc(c.name)}</option>`).join("")}</select></label>
        <label>取钞网点
          <select id="fBranch">${state.meta.branches.map((b) =>
            `<option value="${b.code}">${esc(b.name)}</option>`).join("")}</select></label>
        <label>有效期限（小时）<input id="fHours" type="number" min="1" max="168" value="48"></label>
      </div>
      <div class="denom-editor" id="fDenoms">${denoms("USD")}</div>
      <p class="muted">数量为各券别预约张数；提交即锁定可用库存，可用不足整笔失败。</p>
      <div class="modal-actions">
        <button class="btn ghost" onclick="closeModal()">取消</button>
        <button class="btn lemon" onclick="App.submitCreate()">提交并锁库</button>
      </div>`);
    $("fCcy").onchange = () => { $("fDenoms").innerHTML = denoms($("fCcy").value); };
    $("fBranch").value = currentBranch();
  },

  async submitCreate() {
    const items = {};
    $("fDenoms").querySelectorAll(".denom-qty").forEach((inp) => {
      const n = parseInt(inp.value || "0", 10);
      if (n > 0) items[inp.dataset.did] = n;
    });
    if (!Object.keys(items).length) { flash("请至少填写一个券别数量", "err"); return; }
    try {
      const det = await api("/api/appointments", {
        customer: {
          name: $("fName").value.trim(),
          id_type: $("fIdType").value,
          id_no: $("fIdNo").value.trim(),
          phone: $("fPhone").value.trim(),
        },
        currency: $("fCcy").value,
        branch_code: $("fBranch").value,
        valid_hours: parseInt($("fHours").value || "48", 10),
        items,
      });
      closeModal();
      flash(`预约 ${det.appointment.code} 已创建并锁定库存`);
      await this.loadInventory(); await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  // ------------------------------------------------------------ 预约动作
  async confirmAp(code) {
    try {
      const det = await api(`/api/appointments/${code}/confirm`, {actor: currentTeller()});
      const ev = det.fx_events.find((x) => x.event_type === "cross_day");
      flash(ev
        ? `已按确认日汇率快照（跨日）：1 ${det.appointment.currency_code} = ${det.fx_rate.rate}，折人民币 ${det.appointment.amount_cny_display}`
        : `已确认：折人民币 ${det.appointment.amount_cny_display} 元`);
      await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  async cancelAp(code) {
    const reason = prompt("取消原因（将释放已锁定库存）", "客户行程变更");
    if (reason === null) return;
    try {
      await api(`/api/appointments/${code}/cancel`, {reason, by: "customer"});
      flash(`预约 ${code} 已取消，库存释放`);
      await this.loadInventory(); await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  async runExpireScan() {
    try {
      const r = await api("/api/expire-scan", {});
      flash(r.expired.length ? `已释放 ${r.expired.length} 笔到期预约：${r.expired.join(", ")}`
                             : "没有到期未到场预约", r.expired.length ? "info" : "ok");
      await this.loadInventory(); await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  async decideReplace(code, accept) {
    try {
      await api(`/api/appointments/${code}/replacement/decision`,
        {accept, actor: "customer"});
      flash(accept ? "客户已重新确认，新方案已换锁并重算金额" : "客户拒绝替换，维持原券别");
      await this.loadInventory(); await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  openReplacement(code) {
    api(`/api/appointments/${code}`).then((det) => {
      state.detailCache[code] = det;
      const ccy = det.appointment.currency_code;
      const cur = Object.fromEntries(det.items.map((i) => [i.denomination_id, i.planned_qty]));
      const editor = this.ccyMeta(ccy).denominations.map((d) =>
        `<label>${ccy} ${fmt2(d.face_value)}${cur[d.id] ? "（原 " + cur[d.id] + "）" : ""}
          <input type="number" min="0" step="1" data-did="${d.id}" class="rep-qty"
                 value="${cur[d.id] || 0}"></label>`).join("");
      openModal(`
        <h3>券别替换方案 · ${code}</h3>
        <p class="muted">替换方案需客户重新确认；接受后原子换锁并按当日汇率重算金额。</p>
        <div class="denom-editor">${editor}</div>
        <div class="form-row" style="margin-top:10px">
          <label>替换原因<input id="repReason" value="部分券别库存紧张"></label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" onclick="closeModal()">取消</button>
          <button class="btn lemon" onclick="App.submitReplacement('${code}')">提交客户确认</button>
        </div>`);
    }).catch((e) => flash(e.message, "err"));
  },

  async submitReplacement(code) {
    const items = {};
    $("modalBody").querySelectorAll(".rep-qty").forEach((inp) => {
      const n = parseInt(inp.value || "0", 10);
      if (n > 0) items[inp.dataset.did] = n;
    });
    try {
      await api(`/api/appointments/${code}/replacement`,
        {items, reason: $("repReason").value});
      closeModal();
      flash("替换方案已提出，等待客户重新确认");
      await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  async viewDetail(code) {
    try {
      const d = await api(`/api/appointments/${code}`);
      const rate = d.fx_rate
        ? `1 ${d.appointment.currency_code} = ${d.fx_rate.rate}（${d.fx_rate.rate_date} v${d.fx_rate.version}）`
        : "未确认";
      const fx = d.fx_events.map((e) =>
        `<li>${e.created_at} · ${e.event_type === "cross_day" ? "<b class='cross'>汇率跨日</b>" :
           e.event_type === "reconfirm" ? "替换后重新快照" : "确认快照"}
         · ${e.rate_date || ""} v${e.version || ""} @ ${e.rate || ""} — ${esc(e.detail || "")}</li>`
      ).join("");
      const hist = d.history.map((h) =>
        `<li>${h.created_at} · ${esc(h.face_value)} × ${h.qty}（v${h.plan_version}，${
          {original: "原方案", substitution: "替换", reconfirmed: "客户重新确认"}[h.change_kind]}）</li>`
      ).join("");
      const cancels = d.cancellations.map((c) =>
        `<li>${c.created_at} · ${{customer: "客户取消", teller: "柜员取消", expired: "到期释放"}[c.kind]}：${esc(c.reason)}</li>`
      ).join("");
      openModal(`
        <h3>预约详情 · ${code}</h3>
        <p><span class="badge ${d.appointment.status}">${STATUS_CN[d.appointment.status]}</span>
           方案版本 v${d.appointment.plan_version}</p>
        <div class="fx-snapshot">汇率快照：${rate}</div>
        ${d.appointment.amount_cny ? `<div class="amount-line">外币 ${d.appointment.amount_foreign_display} ｜ <span class="cny">人民币 ${d.appointment.amount_cny_display} 元</span></div>` : ""}
        <h4>汇率轨迹</h4><ul>${fx || "<li class='muted'>无</li>"}</ul>
        <h4>券别方案历史</h4><ul>${hist}</ul>
        ${cancels ? `<h4>取消记录</h4><ul>${cancels}</ul>` : ""}
        <div class="modal-actions"><button class="btn" onclick="closeModal()">关闭</button></div>`);
    } catch (e) { flash(e.message, "err"); }
  },

  // ------------------------------------------------------------ 汇率
  async loadFx() {
    const {rates} = await api("/api/fx");
    $("fxTable").innerHTML =
      `<thead><tr><th>币种</th><th>账务日</th><th class="num">版本</th>
        <th class="num">汇率（兑人民币）</th><th>生效时间</th><th>状态</th><th>发布人</th></tr></thead>
       <tbody>${rates.map((r) => `<tr>
         <td>${r.currency_code} ${esc(r.currency_name)}</td>
         <td>${r.rate_date}</td><td class="num">v${r.version}</td>
         <td class="num mono"><b>${fmt2(r.rate)}</b></td>
         <td>${r.effective_at.replace("T", " ")}</td>
         <td>${r.status === "active"
              ? '<span class="tag-active">生效中</span>'
              : '<span class="tag-superseded">已被替代</span>'}</td>
         <td>${esc(r.created_by)}</td></tr>`).join("")}</tbody>`;
  },

  async publishRate() {
    if (!isSupervisor()) { flash("仅主管可发布汇率版本", "err"); return; }
    try {
      const r = await api("/api/fx/publish", {
        currency: $("pubCurrency").value,
        rate_date: $("pubDate").value,
        rate: $("pubRate").value || null,
      });
      flash(`已发布 ${r.currency} ${r.rate_date} v${r.version}：${r.rate}`);
      $("pubRate").value = "";
      await this.loadFx();
    } catch (e) { flash(e.message, "err"); }
  },

  // ------------------------------------------------------------ 柜面核验
  async runVerify() {
    const code = $("verifyCode").value.trim();
    if (!code) return;
    try {
      const r = await api("/api/verify", {code, counter_branch: currentBranch()});
      $("verifyResult").innerHTML = r.passed
        ? `<div class="verify-box"><span class="verify-ok">✔ 核验通过</span>
             <div class="muted">状态 ${r.status} ｜ 网点 ${r.branch} ｜ 有效期至 ${r.valid_until.replace("T", " ")}</div>
             <button class="btn lemon" style="margin-top:8px" onclick="App.gotoDeliver('${code}')">进入现钞交付</button></div>`
        : `<div class="verify-box"><span class="verify-fail">✘ 核验不通过</span>
             <ul>${r.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>`;
    } catch (e) {
      $("verifyResult").innerHTML =
        `<div class="verify-box"><span class="verify-fail">✘ ${esc(e.message)}</span></div>`;
    }
  },

  gotoDeliver(code) {
    document.querySelector('nav.tabs button[data-page="deliver"]').click();
    $("deliverCode").value = code;
    this.loadDeliver();
  },

  gotoReview() {
    document.querySelector('nav.tabs button[data-page="review"]').click();
  },

  // ------------------------------------------------------------ 交付
  async loadDeliver() {
    const code = $("deliverCode").value.trim();
    if (!code) { $("deliverWrap").innerHTML = ""; return; }
    try {
      const d = await api(`/api/appointments/${code}`);
      state.detailCache[code] = d;
      this.renderDeliver(d);
    } catch (e) {
      $("deliverWrap").innerHTML = `<div class="verify-fail">${esc(e.message)}</div>`;
    }
  },

  renderDeliver(d) {
    const ap = d.appointment;
    if (ap.status !== "CONFIRMED") {
      $("deliverWrap").innerHTML =
        `<div class="muted">预约状态为 <b>${STATUS_CN[ap.status] || ap.status}</b>，${
           ap.status === "DELIVERED" ? "该笔已结清。" : "暂不能交付。"}</div>`;
      return;
    }
    const lines = d.items.map((it) => `
      <tr>
        <td>${ap.currency_code} ${fmt2(it.face_value)}</td>
        <td class="num col-planned mono">${fmt0(it.planned_qty)}</td>
        <td class="num col-counted"><input class="qty mono" type="number" min="0"
             data-did="${it.denomination_id}" data-planned="${it.planned_qty}"
             value="${it.planned_qty}" oninput="App.diffPreview()"></td>
        <td class="num mono diff-cell" data-for="${it.denomination_id}">0</td>
        <td class="num"><label class="muted"><input type="checkbox" class="cf-chk"
             data-did="${it.denomination_id}"> 假钞疑点</label></td>
      </tr>`).join("");
    $("deliverWrap").innerHTML = `
      <div class="fx-snapshot">确认时汇率快照：1 ${ap.currency_code} = ${d.fx_rate.rate}
        （${d.fx_rate.rate_date} v${d.fx_rate.version}）
        ${d.fx_events.some((e) => e.event_type === "cross_day") ? "<span class='cross'>含跨日确认</span>" : ""}
      </div>
      <div class="amount-line">外币 ${ap.amount_foreign_display} ｜
        <span class="cny">人民币 ${ap.amount_cny_display} 元</span>
        <span class="muted">（金额已冻结，柜员不可修改）</span></div>
      <table class="data deliver-table" style="margin-top:10px">
        <thead><tr><th>券别</th><th class="num">预约数量</th><th class="num">实点数量</th>
          <th class="num">差额</th><th class="num">点验标记</th></tr></thead>
        <tbody>${lines}</tbody></table>
      <div class="form-row" style="margin-top:10px">
        <label style="flex:2">备注<input id="deliverNote" placeholder="如：客户同意短款 1 张"></label>
      </div>
      <div class="modal-actions" style="justify-content:flex-start">
        <button class="btn lemon" onclick="App.submitDeliver('${ap.code}')">提交交付（出库）</button>
        <span class="muted">重复提交同一预约为幂等操作，返回原交付单、不重复出库</span>
      </div>`;
    this.diffPreview();
  },

  diffPreview() {
    $("deliverWrap").querySelectorAll(".qty").forEach((inp) => {
      const cell = $(`deliverWrap`).querySelector(`.diff-cell[data-for="${inp.dataset.did}"]`);
      const diff = parseInt(inp.value || "0", 10) - parseInt(inp.dataset.planned, 10);
      cell.textContent = (diff > 0 ? "+" : "") + diff;
      cell.style.color = diff < 0 ? "var(--danger)" : diff > 0 ? "var(--warn)" : "var(--ok)";
      const row = cell.closest("tr");
      row.classList.toggle("diff-row-short", diff < 0);
      row.classList.toggle("diff-row-over", diff > 0);
    });
  },

  async submitDeliver(code) {
    const counted = {}, counterfeit = {};
    $("deliverWrap").querySelectorAll(".qty").forEach((inp) => {
      counted[inp.dataset.did] = parseInt(inp.value || "0", 10);
    });
    $("deliverWrap").querySelectorAll(".cf-chk").forEach((chk) => {
      if (chk.checked)
        counterfeit[chk.dataset.did] = [`CF-${code}-${chk.dataset.did}-001`];
    });
    try {
      const r = await api("/api/deliver", {
        code, counted,
        counterfeit: Object.keys(counterfeit).length ? counterfeit : null,
        note: $("deliverNote").value,
      });
      if (r.replay) {
        flash("该预约已交付过：返回原交付单（幂等，未重复出库）", "info");
      } else if (r.counterfeit) {
        flash("发现假钞疑点，已暂扣登记，需主管处置，本次未出库", "err");
      } else if (r.diffs.some((x) => x.status === "open")) {
        flash("交付完成但存在差额，已转主管差额复核", "info");
      } else {
        flash("交付完成，账实相符");
      }
      await this.loadInventory(); await this.loadCalendar(); await this.loadDiffs();
      this.showDelivery(code, r);
    } catch (e) { flash(e.message, "err"); }
  },

  async showDelivery(code, r0) {
    const d = await api(`/api/appointments/${code}`);
    openModal(`
      <h3>交付结果 · ${code}</h3>
      ${r0?.replay ? `<div class="frozen-note">幂等重放：此为既有交付单，未再次出库。</div>` : ""}
      ${r0?.counterfeit ? `<div class="cf-box">发现假钞疑点，已登记，等待主管处置。</div>` : ""}
      <div class="amount-line">人民币 ${d.appointment.amount_cny_display} 元（金额冻结）</div>
      <div class="muted">交付状态：${esc(r0?.delivery.status || "-")}；可在「差额复核」页查看三栏对照</div>
      <div class="modal-actions"><button class="btn" onclick="closeModal()">关闭</button></div>`);
  },

  // ------------------------------------------------------------ 差额复核
  async loadDiffs() {
    const {diffs} = await api("/api/diffs");
    const badge = $("diffBadge");
    badge.textContent = diffs.length ? `(${diffs.length})` : "";
    badge.style.color = diffs.length ? "var(--lemon)" : "";
    if (!diffs.length) {
      $("reviewWrap").innerHTML = `<div class="card muted">没有待复核的差额记录</div>`;
      return;
    }
    const groups = {};
    diffs.forEach((x) => (groups[x.code] ||= []).push(x));
    $("reviewWrap").innerHTML = Object.entries(groups).map(([code, rows]) => {
      const first = rows[0];
      const trs = rows.map((x) => `<tr class="diff-row-${x.kind === "short" ? "short" : "over"}">
        <td>${x.denomination_id ? esc(x.currency_code) + " " + fmt2(x.face_value) : "—"}</td>
        <td class="num col-planned mono">${fmt0(x.planned_qty)}</td>
        <td class="num col-counted mono">${fmt0(x.counted_qty)}</td>
        <td class="num col-reviewed mono">${fmt0(x.reviewed_qty)}</td>
        <td><span class="badge ${x.kind === "counterfeit" ? "CANCELLED" : "REVIEWED"}">${KIND_CN[x.kind]}</span></td>
        <td class="muted">${esc(x.detail || "")}</td></tr>`).join("");
      return `<div class="card">
        <h3>${code} · ${esc(first.customer_name)}
          <span class="muted">柜员 ${esc(first.delivery_teller || "-")}</span>
          <span class="spacer"></span>
          <button class="btn small ${isSupervisor() ? "lemon" : ""}"
            ${isSupervisor() ? "" : "disabled"}
            onclick="App.openReview('${code}')">
            ${isSupervisor() ? "主管复核" : "仅主管可复核"}</button>
        </h3>
        <table class="data">
          <thead><tr><th>券别</th><th class="num">预约数量</th>
            <th class="num">实点数量</th><th class="num">复核数量</th>
            <th>类型</th><th>说明</th></tr></thead>
          <tbody>${trs}</tbody></table>
      </div>`;
    }).join("");
  },

  async openReview(code) {
    try {
      const d = await api(`/api/appointments/${code}`);
      const hasCf = (await api("/api/diffs")).diffs.some(
        (x) => x.code === code && x.kind === "counterfeit");
      if (hasCf) {
        openModal(`
          <h3>假钞疑点处置 · ${code}</h3>
          <div class="cf-box">该预约存在假钞疑点，实物暂扣，预约尚未出库。</div>
          <div class="form-row"><label style="flex:2">处置说明
            <input id="cfResolution" value="假钞收缴，按反假币规程上报"></label></div>
          <div class="modal-actions">
            <button class="btn ghost" onclick="closeModal()">取消</button>
            <button class="btn danger" onclick="App.resolveCf('${code}')">主管处置并释放库存</button>
          </div>`);
        return;
      }
      const dlMap = Object.fromEntries(
        (d.delivery_lines || []).map((l) => [l.denomination_id, l]));
      const lines = d.items.map((it) => {
        const dl = dlMap[it.denomination_id] || {};
        const counted = dl.counted_qty ?? it.planned_qty;
        return `
        <tr><td>${d.appointment.currency_code} ${fmt2(it.face_value)}</td>
          <td class="num col-planned mono">${fmt0(it.planned_qty)}</td>
          <td class="num col-counted mono">${fmt0(counted)}</td>
          <td class="num col-reviewed"><input class="qty mono" type="number" min="0"
            data-did="${it.denomination_id}" value="${counted}"></td>
          <td class="num mono">${counted - it.planned_qty}</td></tr>`;
      }).join("");
      openModal(`
        <h3>主管差额复核 · ${code}</h3>
        <div class="amount-line">成交金额（已冻结）：人民币 ${d.appointment.amount_cny_display} 元</div>
        <table class="data" style="margin-top:8px">
          <thead><tr><th>券别</th><th class="num">预约数量</th>
            <th class="num">实点数量</th><th class="num">复核数量</th><th class="num">实点差额</th></tr></thead>
          <tbody>${lines}</tbody></table>
        <div class="form-row" style="margin-top:10px">
          <label style="flex:2">复核结论<input id="reviewResolution" value="差额现场确认，账实调整一致"></label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" onclick="closeModal()">取消</button>
          <button class="btn lemon" onclick="App.submitReview('${code}')">确认复核并冻结</button>
        </div>`);
    } catch (e) { flash(e.message, "err"); }
  },

  async submitReview(code) {
    const reviewed = {};
    $("modalBody").querySelectorAll(".qty").forEach((inp) => {
      reviewed[inp.dataset.did] = parseInt(inp.value || "0", 10);
    });
    try {
      const r = await api("/api/review",
        {code, reviewed, resolution: $("reviewResolution").value});
      closeModal();
      flash(r.frozen ? "该交付已复核冻结，不可再修改" : "复核完成，交付单据已冻结");
      await this.loadDiffs(); await this.loadCalendar();
    } catch (e) { flash(e.message, "err"); }
  },

  async resolveCf(code) {
    try {
      await api("/api/counterfeit/resolve",
        {code, resolution: $("cfResolution").value, release_stock: true});
      closeModal();
      flash("假钞疑点已处置，预约取消并释放库存");
      await this.loadDiffs(); await this.loadCalendar(); await this.loadInventory();
    } catch (e) { flash(e.message, "err"); }
  },
};

window.addEventListener("DOMContentLoaded", () =>
  App.init().catch((e) => flash(e.message, "err")));
