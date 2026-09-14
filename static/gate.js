/* 双边门位稳定工作区：逐帧标左右齿孔/片边/缺口，拖放稳定关键帧，预览扫描窗。
   与 app.js 共享 data/params/segId（复算 JSON 是唯一数据来源）。 */
"use strict";

const G_PX_X = 16.0;   // 沿片长 px/mm
const G_PX_Y = 12.0;   // 跨片宽 px/mm（独立纵向比例，角度示意会放大）
const G_MARGIN = 60;

let gFrame = 0, gTool = "Lsprocket";
let gDrag = null;     // 正在拖放的关键帧 {id, mode}

const gc = document.getElementById("gateCanvas");
const gctx = gc.getContext("2d");
const g$ = id => document.getElementById(id);

function gateFrameCount() {
  if (!data) return 1;
  let n = data.frames.length;
  for (const e of data.edge_observations)
    n = Math.max(n, e.frame_index + 1);
  return Math.max(n, 1);
}

/* ---------- 几何（与 compute.py 同式，仅用于即时预览） ---------- */

function gateObsAt(fi, side, kind) {
  return data.edge_observations.filter(
    e => e.frame_index === fi && e.side === side && e.kind === kind);
}

function gateMeasAt(fi) {
  // 取该帧唯一可用观测；多解时返回 ambiguous 标记
  const pick = (side, kind) => {
    const c = gateObsAt(fi, side, kind).filter(e => e.usable);
    return c.length === 1 ? c[0] : null;
  };
  const le = pick("L", "edge"), re = pick("R", "edge");
  const ls = pick("L", "sprocket"), rs = pick("R", "sprocket");
  const m = { width: null, center: null, angle: null, ambiguous: false };
  for (const side of ["L", "R"])
    for (const kind of ["sprocket", "edge"])
      if (gateObsAt(fi, side, kind).filter(e => e.usable).length > 1)
        m.ambiguous = true;
  if (le && re) {
    m.width = params.film_width - le.y - re.y;
    m.center = (re.y - le.y) / 2;
  }
  if (ls && rs) {
    const gauge = params.film_width - (ls.y || 0) - (rs.y || 0);
    m.angle = Math.atan2(rs.x - ls.x, gauge) * 180 / Math.PI;
    if (m.center === null) m.center = ((rs.y || 0) - (ls.y || 0)) / 2;
  }
  return m;
}

function gateCompAt(fi) {
  const f = data.gate.frames.find(x => x.frame === fi);
  return f || { shift: 0, angle: 0 };
}

/* ---------- 坐标映射 ----------
   几何（旋转、角点）统一在 gate-geo.js，入参一律毫米，由纯函数内部
   折像素；本文件不得把毫米先乘 PX_PER_* 再传进去（会二次缩放）。 */

function gOrigin() {
  return { ox: gc.width / 2, oy: gc.height / 2 };
}

/* ---------- 绘制 ---------- */

function drawGate() {
  const W = gc.width, H = gc.height;
  gctx.clearRect(0, 0, W, H);
  if (!data || !params) return;
  const n = gateFrameCount();
  g$("g_count").textContent = n - 1;
  gFrame = Math.max(0, Math.min(n - 1, gFrame));
  if (g$("g_frame") !== document.activeElement) g$("g_frame").value = gFrame;

  const { ox, oy } = gOrigin();
  const pitch = params.nominal_pitch;

  // 片基（标称片宽）
  const fw = params.film_width * G_PX_Y;
  gctx.fillStyle = "#202028";
  gctx.fillRect(0, oy - fw / 2, W, fw);
  gctx.strokeStyle = "#444";
  gctx.strokeRect(0, oy - fw / 2, W, fw);
  gctx.fillStyle = "#666";
  gctx.font = "10px monospace";
  gctx.fillText("左片边 L", 6, oy - fw / 2 - 6);
  gctx.fillText("右片边 R", 6, oy + fw / 2 + 16);

  const m = gateMeasAt(gFrame);
  const comp = gateCompAt(gFrame);
  const resAngle = (m.angle || 0) - comp.angle;
  const dx = (m.center || 0) - comp.shift - params.window_offset;

  // 相邻帧观测（淡显，展示逐帧配对）
  drawNeighborObservations(oy);

  // 扫描窗：轴对齐矩形，跨片宽 = 标称片宽，沿片长 = 窗口尺寸 + 2×安全余量
  const winW = (params.window_size + 2 * params.safe_margin) * G_PX_X;
  gctx.strokeStyle = "#6cf";
  gctx.setLineDash([6, 4]);
  gctx.strokeRect(ox - winW / 2, oy - fw / 2, winW, fw);
  gctx.setLineDash([]);
  gctx.fillStyle = "#6cf";
  gctx.fillText("扫描窗", ox - winW / 2, oy - fw / 2 - 6);

  // 画格：按补偿后残差姿态摆放（实测中心 − 补偿，残差旋角）
  // 半尺寸一律保持毫米，交由 gateFrameCorners 内部折像素
  const picW = (m.width || params.film_width);
  const halfAcross = picW / 2;             // mm
  const halfAlong = params.window_size / 2; // mm
  const corners = gateFrameCorners({
    ox, oy, halfAlong, halfAcross,
    shiftDx: dx, angle: resAngle,
    pxPerX: G_PX_X, pxPerY: G_PX_Y,
  });
  gctx.strokeStyle = "#e8b13c";
  gctx.lineWidth = 1.6;
  gctx.beginPath();
  corners.forEach((p, i) => i ? gctx.lineTo(p.px, p.py)
                               : gctx.moveTo(p.px, p.py));
  gctx.closePath();
  gctx.stroke();
  gctx.lineWidth = 1;

  // 中心十字（补偿目标）
  gctx.strokeStyle = "#7fd07f";
  gctx.beginPath();
  gctx.moveTo(ox - 10, oy); gctx.lineTo(ox + 10, oy);
  gctx.moveTo(ox, oy - 10); gctx.lineTo(ox, oy + 10);
  gctx.stroke();

  // 当前帧观测点
  drawObservations(gFrame, oy, true);

  // 稳定关键帧（所有关键帧沿时间轴投影在顶部轨道，当前帧关键帧可拖）
  drawKeyframeTrack(oy);

  // 信息与余量
  const f = data.gate.frames.find(x => x.frame === gFrame);
  const margin = f && f.min !== null && f.min !== undefined
    ? f.min.toFixed(3) : "—";
  g$("gateInfo").innerHTML =
    "帧 " + gFrame + " · 实测片宽 " + (m.width !== null ? m.width.toFixed(2) : "—")
    + "mm · 中心线横移 " + (m.center !== null ? m.center.toFixed(3) : "—")
    + "mm · 画格旋角 " + (m.angle !== null ? m.angle.toFixed(3) : "—") + "°<br>"
    + "门位补偿：横移 " + comp.shift.toFixed(3) + "mm，旋角 "
    + comp.angle.toFixed(3) + "° · 残差横移 " + dx.toFixed(3)
    + "mm · 最小安全裁切余量 <b>" + margin + "</b>mm"
    + (m.ambiguous ? ' <span style="color:#ff7766">⚠ 同侧同帧多条可用观测（配对多解）</span>' : "");
  g$("gateErr").textContent = (data.gate_errors || []).join("\n");
}

function drawNeighborObservations(oy) {
  for (const e of data.edge_observations) {
    if (e.frame_index === gFrame) continue;
    const d = e.frame_index - gFrame;
    if (Math.abs(d) > 2) continue;
    gctx.globalAlpha = 0.18;
    drawOneObs(e, oy);
    gctx.globalAlpha = 1;
  }
}

function drawObservations(fi, oy, strong) {
  for (const e of data.edge_observations)
    if (e.frame_index === fi) drawOneObs(e, oy);
}

function drawOneObs(e, oy) {
  const { ox } = gOrigin();
  const dx = (e.frame_index - gFrame) * params.nominal_pitch
             + (e.x !== null && e.x !== undefined
                ? e.x - e.frame_index * params.nominal_pitch : 0);
  const ySide = e.side === "L" ? -1 : 1;
  if (!e.usable) {
    // 不可用缺口：红色三角警示
    const px = ox + dx * G_PX_X;
    const py = oy + ySide * params.film_width * G_PX_Y / 2;
    gctx.fillStyle = "#ff5544";
    gctx.beginPath();
    gctx.moveTo(px - 6, py); gctx.lineTo(px + 6, py);
    gctx.lineTo(px, py + ySide * 10); gctx.closePath();
    gctx.fill();
    return;
  }
  const py = oy + ySide * (params.film_width / 2 - (e.y || 0)) * G_PX_Y;
  const px = ox + dx * G_PX_X;
  if (e.kind === "sprocket") {
    gctx.fillStyle = e.side === "L" ? "#7fb8ff" : "#66d9c8";
    gctx.beginPath();
    gctx.arc(px, py, 4, 0, Math.PI * 2);
    gctx.fill();
  } else {
    // 片边：小方块
    gctx.fillStyle = e.side === "L" ? "#9cc8ff" : "#8fe0d2";
    gctx.fillRect(px - 4, py - 4, 8, 8);
  }
}

function drawKeyframeTrack(oy) {
  // 关键帧轨道：画布顶部一排金色菱形；当前帧关键帧在画格上画出补偿把手
  const trackY = 18;
  const n = gateFrameCount();
  for (const kf of data.gate_keyframes) {
    const px = G_MARGIN + (gc.width - 2 * G_MARGIN)
               * kf.frame_index / Math.max(1, n - 1);
    const cur = kf.frame_index === gFrame;
    gctx.fillStyle = cur ? "#ffd766" : "#e8b13c";
    gctx.beginPath();
    gctx.moveTo(px, trackY - 6); gctx.lineTo(px + 6, trackY);
    gctx.lineTo(px, trackY + 6); gctx.lineTo(px - 6, trackY);
    gctx.closePath();
    gctx.fill();
    gctx.fillStyle = "#aaa";
    gctx.font = "9px monospace";
    gctx.fillText(String(kf.frame_index), px - 4, trackY + 20);
    if (!cur) continue;
    // 当前帧关键帧：横移把手（画格中心绿色圆，上下拖）+ 旋角把手（顶部圆）
    const { ox } = gOrigin();
    const comp = gateCompAt(gFrame);
    const kfcy = oy + comp.shift * G_PX_Y;
    gctx.fillStyle = "#7fd07f";
    gctx.beginPath();
    gctx.arc(ox, kfcy, 6, 0, Math.PI * 2);
    gctx.fill();
    // 旋角把手：沿补偿角方向、画格沿片长半幅上方；
    // 与画格角点同式（gateRotateMap，毫米入参），所见即所得
    const reach = params.window_size / 2 * 0.9;   // mm
    const hp = gateRotateMap(0, -reach, ox, kfcy, comp.angle,
                             G_PX_X, G_PX_Y);
    gctx.strokeStyle = "#7fd07f";
    gctx.beginPath();
    gctx.moveTo(ox, kfcy); gctx.lineTo(hp.px, hp.py);
    gctx.stroke();
    gctx.fillStyle = "#ffd766";
    gctx.beginPath();
    gctx.arc(hp.px, hp.py, 6, 0, Math.PI * 2);
    gctx.fill();
  }
}

/* ---------- 交互 ---------- */

function gateNearestObs(fi, px, py) {
  let best = null, bd = 12;
  for (const e of data.edge_observations) {
    if (e.frame_index !== fi) continue;
    const { ox, oy } = gOrigin();
    const ySide = e.side === "L" ? -1 : 1;
    const ex = ox;
    const ey = e.usable
      ? oy + ySide * (params.film_width / 2 - (e.y || 0)) * G_PX_Y
      : oy + ySide * params.film_width * G_PX_Y / 2;
    const d = Math.hypot(px - ex, py - ey);
    if (d < bd) { bd = d; best = e; }
  }
  return best;
}

function gateCurrentKeyframe() {
  return data.gate_keyframes.find(k => k.frame_index === gFrame);
}

gc.addEventListener("click", async ev => {
  if (!data || data.segment.locked) return;
  const rect = gc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;

  if (gTool === "gap") {
    // 缺口标记：落在点击侧（按上下半区），不带坐标
    const { oy } = gOrigin();
    const side = py < oy ? "L" : "R";
    await api("/api/segments/" + segId + "/edges", "POST",
              { frame_index: gFrame, side, kind: "sprocket",
                x: null, y: null, usable: false });
    await loadSegment();
    return;
  }
  if (gTool === "keyframe") {
    const kf = gateCurrentKeyframe();
    if (kf) return;
    const r = await api("/api/segments/" + segId + "/keyframes", "POST",
                        { frame_index: gFrame });
    if (r.status !== 201) alert(r.body.error || "无法插入关键帧");
    await loadSegment();
    return;
  }
  const side = gTool[0];
  const kind = gTool.slice(1);
  const { ox, oy } = gOrigin();
  const ySide = side === "L" ? -1 : 1;
  // y：点击位置反推该侧向内收缩量；x：齿孔沿片长错位量
  const yIn = params.film_width / 2 - ySide * (py - oy) / G_PX_Y;
  const xAlong = gFrame * params.nominal_pitch + (px - ox) / G_PX_X;
  const body = { frame_index: gFrame, side, kind, usable: true,
                 x: kind === "sprocket" ? +xAlong.toFixed(3) : null,
                 y: +yIn.toFixed(3) };
  await api("/api/segments/" + segId + "/edges", "POST", body);
  await loadSegment();
});

gc.addEventListener("mousedown", ev => {
  if (!data || data.segment.locked) return;
  const kf = gateCurrentKeyframe();
  if (!kf || gTool !== "keyframe") return;
  const rect = gc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
  const { ox, oy } = gOrigin();
  const kfcy = oy + kf.shift * G_PX_Y;
  if (Math.hypot(px - ox, py - kfcy) < 10) {
    gDrag = { id: kf.id, mode: "shift" };
  } else {
    // 与绘制把手同一映射（gateRotateMap，毫米入参、内部折像素）
    const reach = params.window_size / 2 * 0.9;
    const hp = gateRotateMap(0, -reach, ox, kfcy, kf.angle,
                             G_PX_X, G_PX_Y);
    if (Math.hypot(px - hp.px, py - hp.py) < 12)
      gDrag = { id: kf.id, mode: "angle" };
  }
});

window.addEventListener("mousemove", async ev => {
  if (!gDrag || !data) return;
  const rect = gc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
  const { ox, oy } = gOrigin();
  const kf = data.gate_keyframes.find(k => k.id === gDrag.id);
  if (!kf) return;
  if (gDrag.mode === "shift") {
    kf.shift = (py - oy) / G_PX_Y;
  } else {
    kf.angle = Math.atan2(px - ox, oy + kf.shift * G_PX_Y - py)
               * 180 / Math.PI;
  }
  drawGate();
});

window.addEventListener("mouseup", async () => {
  if (!gDrag) return;
  const kf = data.gate_keyframes.find(k => k.id === gDrag.id);
  const drag = gDrag;
  gDrag = null;
  if (!kf || data.segment.locked) return;
  const body = drag.mode === "shift" ? { shift: +kf.shift.toFixed(3) }
                                     : { angle: +kf.angle.toFixed(3) };
  const r = await api("/api/segments/" + segId + "/keyframes/" + kf.id,
                      "POST", body);
  if (r.status !== 200) alert(r.body.error || "关键帧更新失败");
  await loadSegment();
});

gc.addEventListener("contextmenu", async ev => {
  ev.preventDefault();
  if (!data || data.segment.locked) return;
  const rect = gc.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
  const kf = gateCurrentKeyframe();
  const { ox, oy } = gOrigin();
  if (kf && gTool === "keyframe"
      && Math.hypot(px - ox, py - (oy + kf.shift * G_PX_Y)) < 12) {
    await api("/api/keyframes/" + kf.id, "DELETE");
    await loadSegment();
    return;
  }
  const obs = gateNearestObs(gFrame, px, py);
  if (obs) {
    await api("/api/edges/" + obs.id, "DELETE");
    await loadSegment();
  }
});

document.querySelectorAll(".gtool").forEach(b =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".gtool").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    gTool = b.dataset.gtool;
  }));

g$("g_prev").onclick = () => { gFrame = Math.max(0, gFrame - 1); drawGate(); };
g$("g_next").onclick = () => {
  gFrame = Math.min(gateFrameCount() - 1, gFrame + 1); drawGate();
};
g$("g_frame").onchange = e => { gFrame = +e.target.value || 0; drawGate(); };
