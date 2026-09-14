/* 双边门位扫描窗预览几何回归：复现角点像素位置。
   直接 require 纯函数模块，不启服务、不依赖浏览器。

       node static/test_gate_geo.js

   断言对应默认参数（16mm 片宽、10.4mm 窗口、G_PX_X=16/G_PX_Y=12、
   1100×320 画布）下画格轮廓落在画布内；修复前四角被重复缩放，
   半幅 83.2px/96px 被再乘一次比例，左上角约 (-781,-992) 出界。
*/
"use strict";
const assert = require("assert");
const { gateRotateMap, gateCornersMm, gateFrameCorners } =
  require("./gate-geo.js");

const PX_X = 16, PX_Y = 12;
const W = 1100, H = 320;
const OX = W / 2, OY = H / 2;
const FILM_W = 16.0, WIN = 10.4;
const HALF_ALONG = WIN / 2, HALF_ACROSS = FILM_W / 2;

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

function assertInside(p, label) {
  assert.ok(p.px > 0 && p.px < W, label + " x=" + p.px + " 出界（重复缩放）");
  assert.ok(p.py > 0 && p.py < H, label + " y=" + p.py + " 出界（重复缩放）");
}

test("gateRotateMap 毫米入参只折一次像素", () => {
  const tl = gateRotateMap(-HALF_ALONG, -HALF_ACROSS, OX, OY, 0,
                           PX_X, PX_Y);
  assert.ok(Math.abs(tl.px - (OX - 5.2 * PX_X)) < 1e-9);
  assert.ok(Math.abs(tl.py - (OY - 8 * PX_Y)) < 1e-9);
});

test("默认数据四角精确位置且在 1100×320 画布内（修复前全部出界）", () => {
  const corners = gateFrameCorners({
    ox: OX, oy: OY, halfAlong: HALF_ALONG, halfAcross: HALF_ACROSS,
    shiftDx: 0, angle: 0, pxPerX: PX_X, pxPerY: PX_Y,
  });
  // 无旋转矩形 = 中心 ± 半幅×比例（毫米只折一次像素），顺序 左上/右上/右下/左下
  const expected = [
    [OX - 5.2 * PX_X, OY - 8 * PX_Y],
    [OX + 5.2 * PX_X, OY - 8 * PX_Y],
    [OX + 5.2 * PX_X, OY + 8 * PX_Y],
    [OX - 5.2 * PX_X, OY + 8 * PX_Y],
  ];
  const labels = ["左上", "右上", "右下", "左下"];
  corners.forEach((p, i) => {
    assertInside(p, labels[i]);
    assert.ok(Math.abs(p.px - expected[i][0]) < 1e-9,
      labels[i] + " x 不可复现");
    assert.ok(Math.abs(p.py - expected[i][1]) < 1e-9,
      labels[i] + " y 不可复现");
  });
});

test("补偿后横移只应用一次比例（dx mm → 像素）", () => {
  const dx = 0.25;
  const corners = gateFrameCorners({
    ox: OX, oy: OY, halfAlong: HALF_ALONG, halfAcross: HALF_ACROSS,
    shiftDx: dx, angle: 0, pxPerX: PX_X, pxPerY: PX_Y,
  });
  for (const p of corners) assertInside(p, "横移后");
  assert.ok(Math.abs(corners[0].py - (OY - 8 * PX_Y + dx * PX_Y)) < 1e-9);
  assert.ok(Math.abs(corners[0].px - (OX - 5.2 * PX_X)) < 1e-9);
});

test("残差旋角下角点位置可复现且在画布内", () => {
  const angle = 1.64;
  const corners = gateFrameCorners({
    ox: OX, oy: OY, halfAlong: HALF_ALONG, halfAcross: HALF_ACROSS,
    shiftDx: 0, angle, pxPerX: PX_X, pxPerY: PX_Y,
  });
  const t = angle * Math.PI / 180;
  const c = Math.cos(t), s = Math.sin(t);
  gateCornersMm(HALF_ALONG, HALF_ACROSS).forEach((q, i) => {
    const ex = OX + (c * q.x + s * q.y) * PX_X;
    const ey = OY + (-s * q.x + c * q.y) * PX_Y;
    assert.ok(Math.abs(corners[i].px - ex) < 1e-9,
      "旋转角点 " + i + " x 不可复现");
    assert.ok(Math.abs(corners[i].py - ey) < 1e-9,
      "旋转角点 " + i + " y 不可复现");
    assertInside(corners[i], "旋转角点 " + i);
  });
});

test("收缩片宽 15.4mm 叠加横移与残差角后轮廓仍在画布内", () => {
  const corners = gateFrameCorners({
    ox: OX, oy: OY, halfAlong: HALF_ALONG, halfAcross: 15.4 / 2,
    shiftDx: 0.11, angle: 1.64, pxPerX: PX_X, pxPerY: PX_Y,
  });
  corners.forEach((p, i) => assertInside(p, "收缩片角点 " + i));
});

let failed = 0;
for (const [name, fn] of tests) {
  try { fn(); console.log("ok -", name); }
  catch (e) { failed++; console.error("FAIL -", name + "\n  " + e.message); }
}
if (failed) { console.error(failed + " 项失败"); process.exit(1); }
console.log("全部 " + tests.length + " 项几何回归通过");
