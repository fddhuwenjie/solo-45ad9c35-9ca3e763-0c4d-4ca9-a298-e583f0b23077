/* 走带编排前端：Canvas 同步条带、齿孔和画格。 */
"use strict";

const PX_PER_MM = 3.2, MARGIN = 30;
const KIND_STYLE = {
  splice:  { color: "#ff5544", label: "接片" },
  notch:   { color: "#ffaa33", label: "缺口" },
  warp:    { color: "#bb77ff", label: "翘曲" },
  brittle: { color: "#ffdd33", label: "脆化边" },
};

let params = null, segments = [], segId = null, data = null;
let tool = "sprocket";

const $ = id => document.getElementById(id);
const strip = $("strip"), ruler = $("ruler");
const sctx = strip.getContext("2d"), rctx = ruler.getContext("2d");

async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: r.status, body: await r.json() };
}

/* ---------- 数据装载 ---------- */

async function loadParams() {
  params = await (await api("/api/params")).body;
  $("p_width").value = params.film_width;
  $("p_pitch").value = params.nominal_pitch;
  $("p_woff").value = params.window_offset;
  $("p_wsize").value = params.window_size;
  $("p_trac").value = params.traction_limit;
  $("version").textContent = "参数版本 v" + params.version;
}

async function loadSegments() {
  segments = (await api("/api/segments")).body;
  const sel = $("segSelect");
  sel.innerHTML = "";
  for (const s of segments) {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = "#" + s.id + " " + s.name + (s.locked ? " 🔒" : "");
    sel.appendChild(o);
  }
  if (segments.length && !segId) segId = segments[0].id;
  if (segId) sel.value = segId;
}

async function loadSegment() {
  if (!segId) { data = null; drawAll(); return; }
  data = (await api("/api/segments/" + segId)).body;
  const locked = data.segment.locked;
  $("lockState").textContent = locked ? "已锁定 · 只读" : "未锁定";
  $("lockState").className = locked ? "locked" : "";
  $("svg").src = "/api/segments/" + segId + "/offset.svg?v=" + data.version;
  renderResults();
  drawAll();
}

function renderResults() {
  if (!data) return;
  const slip = data.first_slip;
  $("slip").textContent = slip
    ? "首个失步：帧 " + slip.frame + "，条带 x=" + slip.x.toFixed(2)
      + "mm，偏移 " + slip.offset.toFixed(3) + "mm"
    : "";
  $("errors").textContent = data.lock_errors.join("\n");
  $("json").textContent = JSON.stringify(
    { version: data.version, first_slip: data.first_slip,
      frames: data.frames.slice(0, 6), lock_errors: data.lock_errors },
    null, 1);
}

/* ---------- 条带绘制 ---------- */

const mm2px = x => MARGIN + x * PX_PER_MM;

function drawAll() {
  drawStrip();
  drawRuler();
}

function drawStrip() {
  const W = strip.width, H = strip.height;
  sctx.clearRect(0, 0, W, H);
  if (!data || !params) return;

  const fw = params.film_width;
  const stripH = Math.min(H - 50, fw * 6);
  const top = (H - stripH) / 2;

  // 片基
  sctx.fillStyle = "#26262c";
  sctx.fillRect(0, top, W, stripH);
  sctx.strokeStyle = "#444";
  sctx.strokeRect(0, top, W, stripH);

  const sprockets = data.measurements.filter(m => m.kind === "sprocket");
  const holeW = 6, holeH = 8;

  // 画格窗口：按标称节拍画参考格，实测位置画实际格
  const winH = stripH - 2 * (holeH + 10);
  sctx.strokeStyle = "#3d3d48";
  for (let x = 0; mm2px(x) < W; x += params.nominal_pitch) {
    sctx.strokeRect(mm2px(x), top + holeH + 10, params.nominal_pitch * PX_PER_MM, winH);
  }
  // 实测画格（随收缩漂移）
  sctx.strokeStyle = "#e8b13c";
  for (const f of data.frames) {
    const px = mm2px(f.x + params.window_offset);
    if (px > W) break;
    sctx.globalAlpha = 0.85;
    sctx.strokeRect(px, top + holeH + 10, params.window_size * PX_PER_MM * 0.7, winH);
  }
  sctx.globalAlpha = 1;

  // 齿孔（实测点）
  for (const m of sprockets) {
    const px = mm2px(m.x);
    if (px > W) continue;
    sctx.fillStyle = "#0c0c0e";
    sctx.strokeStyle = "#888";
    for (const hy of [top + 5, top + stripH - 5 - holeH]) {
      sctx.fillRect(px - holeW / 2, hy, holeW, holeH);
      sctx.strokeRect(px - holeW / 2, hy, holeW, holeH);
    }
    sctx.fillStyle = "#9cf";
    sctx.font = "9px monospace";
    sctx.fillText(m.seq, px - 3, top + stripH + 10);
  }

  // 圈记：接片、缺口、翘曲、脆化边
  for (const m of data.measurements) {
    const st = KIND_STYLE[m.kind];
    if (!st) continue;
    const px = mm2px(m.x);
    sctx.strokeStyle = st.color;
    sctx.lineWidth = 2;
    sctx.beginPath();
    sctx.arc(px, top + stripH / 2, 12, 0, Math.PI * 2);
    sctx.stroke();
    sctx.lineWidth = 1;
    sctx.fillStyle = st.color;
    sctx.font = "10px sans-serif";
    sctx.fillText(st.label, px - 10, top - 6);
  }

  // 锚点
  if (data.segment.anchor_id) {
    const a = data.measurements.find(m => m.id === data.segment.anchor_id);
    if (a) {
      sctx.fillStyle = "#7fd07f";
      sctx.fillText("⚓锚", mm2px(a.x) - 8, top + stripH + 24);
    }
  }

  // 首个失步位置钉回条带
  if (data.first_slip) {
    const px = mm2px(data.first_slip.x);
    sctx.strokeStyle = "#ff5544";
    sctx.lineWidth = 2;
    sctx.beginPath();
    sctx.moveTo(px, top - 14);
    sctx.lineTo(px, top + stripH + 14);
    sctx.stroke();
    sctx.lineWidth = 1;
    sctx.fillStyle = "#ff5544";
    sctx.fillText("📌失步", px - 14, top - 18);
  }

  // 锁定遮罩提示
  if (data.segment.locked) {
    sctx.fillStyle = "rgba(127,208,127,0.06)";
    sctx.fillRect(0, top, W, stripH);
  }
}

/* ---------- 时间尺 ---------- */

function drawRuler() {
  const W = ruler.width, H = ruler.height;
  rctx.clearRect(0, 0, W, H);
  if (!data || !data.frames.length) return;
  const n = data.frames.length;
  const pxPerFrame = (W - 2 * MARGIN) / Math.max(1, n);

  rctx.strokeStyle = "#555";
  rctx.beginPath();
  rctx.moveTo(MARGIN, H - 20);
  rctx.lineTo(W - MARGIN, H - 20);
  rctx.stroke();

  // 帧刻度 + 校正时码
  rctx.fillStyle = "#888";
  rctx.font = "9px monospace";
  for (let i = 0; i < n; i += Math.ceil(n / 40)) {
    const x = MARGIN + i * pxPerFrame;
    rctx.fillRect(x, H - 24, 1, 8);
    rctx.fillText(data.frames[i].tc.toFixed(2), x - 8, H - 28);
  }

  // 偏移微缩曲线
  rctx.strokeStyle = "#e8b13c";
  rctx.beginPath();
  data.frames.forEach((f, i) => {
    const x = MARGIN + i * pxPerFrame;
    const y = 14 - Math.max(-12, Math.min(12, f.offset * 4));
    i ? rctx.lineTo(x, y) : rctx.moveTo(x, y);
  });
  rctx.stroke();

  // 动作标记
  const icon = { slow: "降", skip: "空", hold: "停" };
  for (const a of data.actions) {
    const x = MARGIN + a.frame_index * pxPerFrame;
    rctx.fillStyle = { slow: "#6cf", skip: "#fa6", hold: "#f66" }[a.type];
    rctx.beginPath();
    rctx.arc(x, H - 20, 7, 0, Math.PI * 2);
    rctx.fill();
    rctx.fillStyle = "#111";
    rctx.fillText(icon[a.type] || "?", x - 4, H - 17);
  }
}

/* ---------- 交互 ---------- */

strip.addEventListener("click", async e => {
  if (!data) return;
  const rect = strip.getBoundingClientRect();
  const x = (e.clientX - rect.left - MARGIN) / PX_PER_MM;
  if (x < 0) return;
  if (tool === "anchor") {
    // 设锚点：锁定段也允许，只续算锚点后方
    let best = null, bd = Infinity;
    for (const m of data.measurements) {
      if (m.kind !== "sprocket") continue;
      const d = Math.abs(m.x - x);
      if (d < bd) { bd = d; best = m; }
    }
    if (best && bd < 10) {
      await api("/api/segments/" + segId + "/anchor", "POST",
                { measurement_id: best.id });
      await loadSegment();
    }
    return;
  }
  if (data.segment.locked) return;
  const seq = tool === "sprocket"
    ? data.measurements.filter(m => m.kind === "sprocket").length + 1
    : 0;
  await api("/api/segments/" + segId + "/measurements", "POST",
            { seq, x, y: 0, kind: tool });
  await loadSegment();
});

$("clearAnchor").onclick = async () => {
  if (!data) return;
  await api("/api/segments/" + segId + "/anchor", "POST",
            { measurement_id: null });
  await loadSegment();
};

strip.addEventListener("contextmenu", async e => {
  e.preventDefault();
  if (!data || data.segment.locked || !data.measurements.length) return;
  const rect = strip.getBoundingClientRect();
  const x = (e.clientX - rect.left - MARGIN) / PX_PER_MM;
  let best = null, bd = Infinity;
  for (const m of data.measurements) {
    const d = Math.abs(m.x - x);
    if (d < bd) { bd = d; best = m; }
  }
  if (best && bd < 10) {
    await api("/api/measurements/" + best.id, "DELETE");
    await loadSegment();
  }
});

document.querySelectorAll(".tool").forEach(b =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tool").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    tool = b.dataset.kind;
  }));

$("saveParams").onclick = async () => {
  await api("/api/params", "POST", {
    film_width: +$("p_width").value,
    nominal_pitch: +$("p_pitch").value,
    window_offset: +$("p_woff").value,
    window_size: +$("p_wsize").value,
    traction_limit: +$("p_trac").value,
  });
  await loadParams();
  await loadSegment();
};

$("addSeg").onclick = async () => {
  const r = await api("/api/segments", "POST", { name: $("newSegName").value });
  segId = r.body.id;
  await loadSegments();
  await loadSegment();
};

$("segSelect").onchange = async e => { segId = +e.target.value; await loadSegment(); };

$("lockSeg").onclick = async () => {
  const r = await api("/api/segments/" + segId + "/lock", "POST", {});
  if (r.status === 422) alert("不允许锁定：\n" + r.body.errors.join("\n"));
  await loadSegments();
  await loadSegment();
};

$("unlockSeg").onclick = async () => {
  await api("/api/segments/" + segId + "/unlock", "POST", {});
  await loadSegments();
  await loadSegment();
};

$("addAction").onclick = async () => {
  if (!data || data.segment.locked) return;
  await api("/api/segments/" + segId + "/actions", "POST", {
    frame_index: +$("a_frame").value || 0,
    type: $("a_type").value,
    factor: +$("a_factor").value || null,
    span: +$("a_span").value || 1,
    resume_tc: $("a_resume").value === "" ? null : +$("a_resume").value,
  });
  await loadSegment();
};

$("recompute").onclick = loadSegment;

/* ---------- 启动 ---------- */

(async () => {
  await loadParams();
  await loadSegments();
  await loadSegment();
})();
