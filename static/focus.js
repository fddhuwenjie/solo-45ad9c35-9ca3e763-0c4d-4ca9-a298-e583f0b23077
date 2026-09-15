/* 焦面排程工作区：在 Canvas 画格上录中央/四角/接片前后测高，时间轴上
   布置焦点锚点，对照恒定/推荐/人工三种策略，即时预览翘曲包络、景深窗、
   清晰覆盖与电机速度。与 app.js 共享 data/params/segId（复算 JSON 唯一
   数据来源），逐帧几何（画格角点）复用 gate-geo.js 的纯函数。 */
"use strict";

const F_PX_X = 16.0;    // 沿片长 px/mm
const F_PX_Y = 12.0;    // 跨片宽 px/mm
const FOCUS_POS_CN = {
  C: "中央", TL: "左上", TR: "右上", BL: "左下", BR: "右下",
  SB: "接片前", SA: "接片后",
};
const FOCUS_POS_COLOR = {
  C: "#ffd766", TL: "#7fb8ff", TR: "#8fe0d2", BL: "#c9a0ff", BR: "#ffb077",
  SB: "#ff7766", SA: "#ff9ecb",
};
// 五点在画格局部坐标（相对画格半尺寸的比例；y 跨片宽向下为右），
// 锚点把手与角点复用 gate-geo 的毫米映射
const POS_MM = {
  C:  { x: 0.0, y: 0.0 },
  TL: { x: -0.86, y: -0.82 },
  TR: { x: 0.86, y: -0.82 },
  BL: { x: -0.86, y: 0.82 },
  BR: { x: 0.86, y: 0.82 },
};
const FOCUS_MIN_COVERAGE = 0.8;

let fFrame = 0, fTool = "C", fDrag = null;

const fc = document.getElementById("focusCanvas");
const fctx = fc.getContext("2d");
const f$ = id => document.getElementById(id);

function focusFrameCount() {
  if (!data) return 1;
  let n = data.frames.length;
  for (const h of data.height_observations) n = Math.max(n, h.frame_index + 1);
  for (const e of data.edge_observations) n = Math.max(n, e.frame_index + 1);
  return Math.max(n, 1);
}

function fOrigin() { return { ox: fc.width / 2, oy: fc.height / 2 - 8 }; }

/* ---------- 数据查询（与 compute.py 同式，仅用于即时预览） ---------- */

function fUsable() {
  return data.height_observations.filter(h => h.usable && h.z !== null);
}

function fHeightAt(fi, pos) {
  // 同帧多点位取均值（多解另由校验拦截），帧间线性插值，片段外夹端点
  const pts = fUsable().filter(h => h.pos === pos)
    .map(h => ({ fi: h.frame_index, z: h.z }))
    .sort((a, b) => a.fi - b.fi);
  if (!pts.length) return null;
  const here = pts.filter(p => p.fi === fi);
  if (here.length) return here.reduce((s, p) => s + p.z, 0) / here.length;
  if (fi <= pts[0].fi) return pts[0].z;
  if (fi >= pts[pts.length - 1].fi) return pts[pts.length - 1].z;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1];
    if (a.fi < fi && fi < b.fi) {
      const t = (fi - a.fi) / (b.fi - a.fi);
      return a.z + (b.z - a.z) * t;
    }
  }
  return null;
}

function fEnvelope(fi) {
  const byPos = {};
  for (const p of ["C", "TL", "TR", "BL", "BR"])
    byPos[p] = fHeightAt(fi, p);
  const vals = Object.values(byPos).filter(v => v !== null);
  if (!vals.length) return null;
  return { near: Math.min(...vals), far: Math.max(...vals), byPos };
}

function fFocusRow(fi) {
  return data.focus.frames.find(r => r.frame === fi) || null;
}

function fAmbiguous(fi) {
  return (data.focus.ambiguous_frames || []).includes(fi);
}

/* ---------- 坐标映射 ---------- */

function posCanvasPx(fi, pos) {
  // 五点在画格上的像素位置：相对画格半尺寸（窗口尺寸/2、片宽/2）落到 86%/82%
  const { ox, oy } = fOrigin();
  const halfAlong = params.window_size / 2;
  const halfAcross = (params.film_width) / 2;
  const q = POS_MM[pos];
  return gateRotateMap(q.x * halfAlong, q.y * halfAcross, ox, oy, 0,
                       F_PX_X, F_PX_Y);
}

/* ---------- 绘制 ---------- */

function drawFocus() {
  const W = fc.width, H = fc.height;
  fctx.clearRect(0, 0, W, H);
  if (!data || !params) return;
  const n = focusFrameCount();
  f$("f_count").textContent = n - 1;
  fFrame = Math.max(0, Math.min(n - 1, fFrame));
  if (f$("f_frame") !== document.activeElement) f$("f_frame").value = fFrame;

  // 策略按钮态
  document.querySelectorAll(".strat").forEach(b =>
    b.classList.toggle("active", b.dataset.strat === data.focus.strategy));

  const { ox, oy } = fOrigin();
  const fwPx = params.film_width * F_PX_Y;

  // 片基 + 扫描窗
  fctx.fillStyle = "#202028";
  fctx.fillRect(0, oy - fwPx / 2, W, fwPx);
  fctx.strokeStyle = "#444";
  fctx.strokeRect(0, oy - fwPx / 2, W, fwPx);
  const winW = (params.window_size + 2 * params.safe_margin) * F_PX_X;
  fctx.strokeStyle = "#6cf";
  fctx.setLineDash([6, 4]);
  fctx.strokeRect(ox - winW / 2, oy - fwPx / 2, winW, fwPx);
  fctx.setLineDash([]);

  // 画格轮廓（复用门位角点几何，无旋焦简化为零角）
  const corners = gateFrameCorners({
    ox, oy,
    halfAlong: params.window_size / 2,
    halfAcross: params.film_width / 2,
    shiftDx: 0, angle: 0, pxPerX: F_PX_X, pxPerY: F_PX_Y,
  });
  fctx.strokeStyle = "#e8b13c";
  fctx.lineWidth = 1.5;
  fctx.beginPath();
  corners.forEach((p, i) => i ? fctx.lineTo(p.px, p.py)
                               : fctx.moveTo(p.px, p.py));
  fctx.closePath();
  fctx.stroke();
  fctx.lineWidth = 1;

  // 五个标准点位虚影（提示录入位置）
  for (const pos of ["C", "TL", "TR", "BL", "BR"]) {
    const p = posCanvasPx(fFrame, pos);
    fctx.strokeStyle = "rgba(255,255,255,0.18)";
    fctx.beginPath();
    fctx.arc(p.px, p.py, 9, 0, Math.PI * 2);
    fctx.stroke();
    fctx.fillStyle = "rgba(255,255,255,0.3)";
    fctx.font = "9px monospace";
    fctx.fillText(FOCUS_POS_CN[pos], p.px + 10, p.py - 8);
  }

  // 包络/景深信息在下方信息栏；画格上画实测点（同帧多条即歧义红点）
  drawFocusPoints(fFrame);

  // 焦点锚点轨道（顶部）+ 当前帧锚点拖把手
  drawFocusAnchorTrack(oy);

  // 焦位/速度/覆盖信息
  renderFocusInfo();
}

function drawFocusPoints(fi) {
  const groups = {};
  for (const h of data.height_observations) {
    if (h.frame_index !== fi) continue;
    if (h.pos === "SB" || h.pos === "SA") continue;   // 接片点画在接缝两侧
    (groups[h.pos] = groups[h.pos] || []).push(h);
  }
  for (const pos in groups) {
    const hs = groups[pos].filter(h => h.usable);
    const p = posCanvasPx(fi, pos);
    const multi = hs.length > 1;
    fctx.fillStyle = multi ? "#ff5544" : FOCUS_POS_COLOR[pos];
    fctx.beginPath();
    fctx.arc(p.px, p.py, multi ? 7 : 5, 0, Math.PI * 2);
    fctx.fill();
    if (hs.length) {
      const z = hs.reduce((s, h) => s + h.z, 0) / hs.length;
      fctx.fillStyle = "#ddd";
      fctx.font = "10px monospace";
      fctx.fillText(z.toFixed(2), p.px + 10, p.py + 4);
    }
  }
  // 废读点：灰叉
  for (const h of data.height_observations) {
    if (h.frame_index !== fi || h.usable ||
        !(h.pos in POS_MM)) continue;
    const p = posCanvasPx(fi, h.pos);
    fctx.strokeStyle = "#777";
    fctx.beginPath();
    fctx.moveTo(p.px - 5, p.py - 5); fctx.lineTo(p.px + 5, p.py + 5);
    fctx.moveTo(p.px + 5, p.py - 5); fctx.lineTo(p.px - 5, p.py + 5);
    fctx.stroke();
  }
  // 接片前后基准：画在扫描窗左/右外沿
  for (const [pos, dxSide] of [["SB", -1], ["SA", 1]]) {
    const hs = data.height_observations.filter(
      h => h.frame_index === fi && h.pos === pos && h.usable);
    if (!hs.length) continue;
    const { ox, oy } = fOrigin();
    const px = ox + dxSide * (params.window_size / 2 + params.safe_margin)
               * F_PX_X;
    const z = hs.reduce((s, h) => s + h.z, 0) / hs.length;
    fctx.fillStyle = FOCUS_POS_COLOR[pos];
    fctx.beginPath();
    fctx.moveTo(px, oy - 8); fctx.lineTo(px + dxSide * 10, oy);
    fctx.lineTo(px, oy + 8); fctx.closePath();
    fctx.fill();
    fctx.fillStyle = "#ddd";
    fctx.font = "10px monospace";
    fctx.fillText(pos + " " + z.toFixed(2), px - 14, oy - 12);
  }
}

function drawFocusAnchorTrack(oy) {
  const trackY = 18;
  const n = focusFrameCount();
  // 时间轴上的焦位轨迹缩略（金）与包络区间（淡蓝竖线）
  const rows = data.focus.frames;
  const zvals = rows.flatMap(r => [r.z_near, r.z_far, r.focus])
    .filter(v => v !== null && v !== undefined);
  if (zvals.length) {
    const zlo = Math.min(...zvals), zhi = Math.max(...zvals);
    const tx = fi => 60 + (fc.width - 120) * fi / Math.max(1, n - 1);
    const tz = z => trackY + 26 - 22 * (z - zlo) / Math.max(1e-9, zhi - zlo);
    fctx.strokeStyle = "rgba(127,184,255,0.5)";
    for (const r of rows) {
      if (r.z_near === null) continue;
      fctx.beginPath();
      fctx.moveTo(tx(r.frame), tz(r.z_far));
      fctx.lineTo(tx(r.frame), tz(r.z_near));
      fctx.stroke();
    }
    fctx.strokeStyle = "#e8b13c";
    fctx.beginPath();
    rows.forEach((r, i) => {
      if (r.focus === null || r.skipped) return;
      const x = tx(r.frame), y = tz(r.focus);
      i ? fctx.lineTo(x, y) : fctx.moveTo(x, y);
    });
    fctx.stroke();
    // 覆盖不足帧红 tick
    for (const r of rows) {
      if (r.coverage !== null && r.coverage < FOCUS_MIN_COVERAGE) {
        fctx.fillStyle = "#ff5544";
        fctx.fillRect(tx(r.frame) - 1, trackY + 28, 2, 6);
      }
    }
  }
  for (const a of data.focus_anchors) {
    const px = 60 + (fc.width - 120) * a.frame_index / Math.max(1, n - 1);
    const cur = a.frame_index === fFrame;
    fctx.fillStyle = cur ? "#a8ffb0" : "#7fd07f";
    fctx.beginPath();
    fctx.arc(px, trackY, cur ? 6 : 4.5, 0, Math.PI * 2);
    fctx.fill();
    fctx.fillStyle = "#aaa";
    fctx.font = "9px monospace";
    fctx.fillText(String(a.frame_index), px - 4, trackY + 44);
  }
  // 当前帧锚点：画格中心绿色把手，上下拖动改焦位（纵向像素映射物距差）
  const a = data.focus_anchors.find(x => x.frame_index === fFrame);
  if (a) {
    const { ox } = fOrigin();
    // 物距差按 6 px/mm 纵展（与画格几何比例独立，仅作焦位拖拽手感）
    const py = oy - 60 - (a.focus - (params.focus_near + params.focus_far) / 2) * 6;
    fctx.strokeStyle = "#7fd07f";
    fctx.beginPath();
    fctx.moveTo(ox, oy); fctx.lineTo(ox, py);
    fctx.stroke();
    fctx.fillStyle = "#a8ffb0";
    fctx.beginPath();
    fctx.arc(ox, py, 7, 0, Math.PI * 2);
    fctx.fill();
    fctx.fillStyle = "#7fd07f";
    fctx.font = "10px monospace";
    fctx.fillText("焦位 " + a.focus.toFixed(2), ox + 10, py + 3);
    a._handlePy = py;
  }
}

function renderFocusInfo() {
  const env = fEnvelope(fFrame);
  const r = fFocusRow(fFrame);
  const zlo = Math.min(params.focus_near, params.focus_far);
  const zhi = Math.max(params.focus_near, params.focus_far);
  let html = "帧 " + fFrame + " · 策略 <b>"
    + { constant: "恒定", recommended: "推荐", manual: "人工" }[data.focus.strategy]
    + "</b> · 测高稿 " + data.focus.h_sig;
  if (env) {
    html += "<br>翘曲包络 " + env.near.toFixed(3) + "–" + env.far.toFixed(3)
      + "mm（厚 " + (env.far - env.near).toFixed(3) + "）· 景深 "
      + params.lens_dof.toFixed(2) + "mm · 调焦范围 "
      + zlo.toFixed(1) + "–" + zhi.toFixed(1);
  } else {
    html += "<br>该帧无测高（区间插值外推或空档）";
  }
  if (r) {
    html += "<br>所需焦位 " + (r.target === null ? "—" : r.target.toFixed(3))
      + "mm · 下达焦位 <b>" + (r.focus === null ? "—" : r.focus.toFixed(3))
      + "</b>mm · 电机速度 " + (r.dt === null ? "—"
        : (r.speed === Infinity ? "∞" : r.speed.toFixed(2)))
      + "mm/s · 静定 " + (r.settled ? "✓" : '<span style="color:#ff7766">✗ 来不及</span>')
      + " · 清晰覆盖 <b>" + (r.coverage === null ? "—（空齿）"
        : (100 * r.coverage).toFixed(0) + "%") + "</b>";
  }
  if (fAmbiguous(fFrame))
    html += ' <span style="color:#ff7766">⚠ 同点位多条可用测高（归帧歧义）</span>';
  f$("focusInfo").innerHTML = html;
  f$("focusErr").textContent = (data.focus_errors || []).join("\n");
}

/* ---------- 交互 ---------- */

fc.addEventListener("click", async ev => {
  if (!data || data.segment.locked) return;
  const rect = fc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;

  if (fTool === "fanchor") {
    if (data.focus_anchors.some(a => a.frame_index === fFrame)) return;
    const r = await api("/api/segments/" + segId + "/focus_anchors", "POST",
                       { frame_index: fFrame });
    if (r.status !== 201) alert(r.body.error || "无法插入焦点锚点");
    await loadSegment();
    return;
  }
  // 标准点位：找最近虚影；SB/SA 吸附到扫描窗左/右外沿三角
  let best = null, bd = 16;
  if (fTool === "SB" || fTool === "SA") {
    const { ox, oy } = fOrigin();
    const dxSide = fTool === "SB" ? -1 : 1;
    const sx = ox + dxSide * (params.window_size / 2 + params.safe_margin)
               * F_PX_X;
    if (Math.abs(px - sx) < 24 && Math.abs(py - oy) < 24) best = fTool;
  } else {
    for (const pos of ["C", "TL", "TR", "BL", "BR"]) {
      const p = posCanvasPx(fFrame, pos);
      const d = Math.hypot(px - p.px, py - p.py);
      if (d < bd) { bd = d; best = pos; }
    }
  }
  if (best !== fTool) return;
  const prev = fUsable().filter(h => h.frame_index === fFrame && h.pos === fTool);
  const def = prev.length ? prev[prev.length - 1].z
    : (data.focus.frames.find(r => r.frame === fFrame) || {}).mid
      || (params.focus_near + params.focus_far) / 2;
  const zstr = prompt("测点 " + FOCUS_POS_CN[fTool] + "（帧 " + fFrame
                      + "）物距 mm：", String(+def.toFixed(3)));
  if (zstr === null) return;
  const z = parseFloat(zstr);
  if (isNaN(z)) return;
  const r = await api("/api/segments/" + segId + "/heights", "POST",
                      { frame_index: fFrame, pos: fTool, z, usable: true });
  if (r.status !== 201) alert(r.body.error || "测高录入失败");
  await loadSegment();
});

fc.addEventListener("mousedown", ev => {
  if (!data || data.segment.locked || fTool !== "fanchor") return;
  const a = data.focus_anchors.find(x => x.frame_index === fFrame);
  if (!a || a._handlePy === undefined) return;
  const rect = fc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
  const { ox } = fOrigin();
  if (Math.hypot(px - ox, py - a._handlePy) < 12) fDrag = { id: a.id };
});

window.addEventListener("mousemove", ev => {
  if (!fDrag || !data) return;
  const rect = fc.getBoundingClientRect();
  const py = ev.clientY - rect.top;
  const { oy } = fOrigin();
  const a = data.focus_anchors.find(x => x.id === fDrag.id);
  if (!a) return;
  const mid = (params.focus_near + params.focus_far) / 2;
  a.focus = (oy - 60 - py) / 6 + mid;
  drawFocus();
});

window.addEventListener("mouseup", async () => {
  if (!fDrag) return;
  const a = data.focus_anchors.find(x => x.id === fDrag.id);
  const drag = fDrag;
  fDrag = null;
  if (!a || data.segment.locked) return;
  const lo = Math.min(params.focus_near, params.focus_far);
  const hi = Math.max(params.focus_near, params.focus_far);
  const focus = Math.min(hi, Math.max(lo, a.focus));
  const r = await api("/api/segments/" + segId + "/focus_anchors/" + a.id,
                      "POST", { focus: +focus.toFixed(3) });
  if (r.status !== 200) alert(r.body.error || "焦点锚点更新失败");
  await loadSegment();
});

fc.addEventListener("contextmenu", async ev => {
  ev.preventDefault();
  if (!data || data.segment.locked) return;
  const rect = fc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
  // 优先删当前帧焦点锚点把手
  const a = data.focus_anchors.find(x => x.frame_index === fFrame);
  const { ox } = fOrigin();
  if (a && a._handlePy !== undefined
      && Math.hypot(px - ox, py - a._handlePy) < 12) {
    await api("/api/focus_anchors/" + a.id, "DELETE");
    await loadSegment();
    return;
  }
  // 删最近的测高点
  let best = null, bd = 12;
  for (const h of data.height_observations) {
    if (h.frame_index !== fFrame) continue;
    let hp;
    if (h.pos in POS_MM) hp = posCanvasPx(fFrame, h.pos);
    else {
      const dxSide = h.pos === "SB" ? -1 : 1;
      hp = { px: ox + dxSide * (params.window_size / 2 + params.safe_margin)
                    * F_PX_X, py: oy };
    }
    const d = Math.hypot(px - hp.px, py - hp.py);
    if (d < bd) { bd = d; best = h; }
  }
  if (best) {
    await api("/api/heights/" + best.id, "DELETE");
    await loadSegment();
  }
});

document.querySelectorAll(".ftool").forEach(b =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".ftool").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    fTool = b.dataset.ftool;
  }));

document.querySelectorAll(".strat").forEach(b =>
  b.addEventListener("click", async () => {
    if (!data || b.dataset.strat === data.focus.strategy) return;
    const r = await api("/api/segments/" + segId + "/focus_strategy", "POST",
                        { strategy: b.dataset.strat });
    if (r.status !== 200) alert(r.body.error || "策略切换失败");
    await loadSegment();
  }));

f$("f_prev").onclick = () => { fFrame = Math.max(0, fFrame - 1); drawFocus(); };
f$("f_next").onclick = () => {
  fFrame = Math.min(focusFrameCount() - 1, fFrame + 1); drawFocus();
};
f$("f_frame").onchange = e => { fFrame = +e.target.value || 0; drawFocus(); };
