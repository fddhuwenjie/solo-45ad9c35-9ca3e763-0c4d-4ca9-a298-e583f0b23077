/* 双边门位 Canvas 几何：纯函数，无 DOM 依赖，供 gate.js 绘制与
   Node 回归测试（test_gate_geo.js）共用。

   约定：入参一律毫米（x 沿片长，y 跨片宽向下为右），
   gateRotateMap 内部按 pxPerX/pxPerY 折成画布像素——调用方不得
   先把毫米换算成像素再传入，否则会被二次缩放（角点飞出画布）。 */
"use strict";

function gateRotateMap(x, y, cx, cy, angDeg, pxPerX, pxPerY) {
  const t = angDeg * Math.PI / 180;
  const c = Math.cos(t), s = Math.sin(t);
  return {
    px: cx + (c * x + s * y) * pxPerX,
    py: cy + (-s * x + c * y) * pxPerY,
  };
}

// 画格四角局部坐标（毫米），顺序 左上 / 右上 / 右下 / 左下
function gateCornersMm(halfAlong, halfAcross) {
  return [
    { x: -halfAlong, y: -halfAcross },
    { x: halfAlong, y: -halfAcross },
    { x: halfAlong, y: halfAcross },
    { x: -halfAlong, y: halfAcross },
  ];
}

// 画格四角画布坐标。
// opts: {ox,oy} 原点像素；halfAlong/halfAcross/shiftDx 毫米；
//       angle 度（补偿后残差角）；pxPerX/pxPerY 每毫米像素。
function gateFrameCorners(opts) {
  const cy = opts.oy + opts.shiftDx * opts.pxPerY;
  return gateCornersMm(opts.halfAlong, opts.halfAcross).map(p =>
    gateRotateMap(p.x, p.y, opts.ox, cy, opts.angle,
                  opts.pxPerX, opts.pxPerY));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { gateRotateMap, gateCornersMm, gateFrameCorners };
}
