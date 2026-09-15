"""走带编排服务：Python 标准库承接请求，sqlite3 存测量稿。

    python3 server.py [port]     默认 8000
"""

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import compute

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "measurements.db")
STATIC = os.path.join(BASE, "static")

SCHEMA = """
CREATE TABLE IF NOT EXISTS params (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    film_width REAL NOT NULL DEFAULT 16.0,
    nominal_pitch REAL NOT NULL DEFAULT 7.62,
    window_offset REAL NOT NULL DEFAULT 0.0,
    window_size REAL NOT NULL DEFAULT 10.4,
    traction_limit REAL NOT NULL DEFAULT 1.5,
    safe_margin REAL NOT NULL DEFAULT 0.3,
    lens_dof REAL NOT NULL DEFAULT 0.25,
    focus_near REAL NOT NULL DEFAULT 40.0,
    focus_far REAL NOT NULL DEFAULT 60.0,
    motor_speed REAL NOT NULL DEFAULT 8.0,
    settle_time REAL NOT NULL DEFAULT 0.02,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    anchor_id INTEGER,
    compute_cache TEXT,
    focus_strategy TEXT NOT NULL DEFAULT 'recommended'
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    seq INTEGER NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'sprocket'
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    frame_index INTEGER NOT NULL,
    type TEXT NOT NULL,
    factor REAL,
    span INTEGER DEFAULT 1,
    resume_tc REAL
);
CREATE TABLE IF NOT EXISTS edge_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    frame_index INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('L','R')),
    kind TEXT NOT NULL CHECK (kind IN ('sprocket','edge')),
    x REAL,
    y REAL,
    usable INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS gate_keyframes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    frame_index INTEGER NOT NULL,
    shift REAL NOT NULL,
    angle REAL NOT NULL DEFAULT 0.0,
    UNIQUE (segment_id, frame_index)
);
CREATE TABLE IF NOT EXISTS height_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    frame_index INTEGER NOT NULL,
    pos TEXT NOT NULL,
    z REAL,
    usable INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS focus_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    frame_index INTEGER NOT NULL,
    focus REAL NOT NULL,
    UNIQUE (segment_id, frame_index)
);
"""


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    try:  # 旧库迁移
        conn.execute("ALTER TABLE actions ADD COLUMN resume_tc REAL")
    except sqlite3.OperationalError:
        pass
    try:  # 双边门位：安全裁切余量
        conn.execute("ALTER TABLE params ADD COLUMN safe_margin REAL "
                     "NOT NULL DEFAULT 0.3")
    except sqlite3.OperationalError:
        pass
    try:  # 双边门位：区间补偿缓存
        conn.execute("ALTER TABLE segments ADD COLUMN gate_cache TEXT")
    except sqlite3.OperationalError:
        pass
    # 焦面排程：镜头景深、调焦范围、电机速度、静定时长
    for col, ddl in (
            ("lens_dof", "REAL NOT NULL DEFAULT 0.25"),
            ("focus_near", "REAL NOT NULL DEFAULT 40.0"),
            ("focus_far", "REAL NOT NULL DEFAULT 60.0"),
            ("motor_speed", "REAL NOT NULL DEFAULT 8.0"),
            ("settle_time", "REAL NOT NULL DEFAULT 0.02")):
        try:
            conn.execute("ALTER TABLE params ADD COLUMN %s %s" % (col, ddl))
        except sqlite3.OperationalError:
            pass
    try:  # 焦面排程：策略选择（恒定/推荐/人工）
        conn.execute("ALTER TABLE segments ADD COLUMN focus_strategy TEXT"
                     " NOT NULL DEFAULT 'recommended'")
    except sqlite3.OperationalError:
        pass
    try:  # 焦面排程：区间排程缓存
        conn.execute("ALTER TABLE segments ADD COLUMN focus_cache TEXT")
    except sqlite3.OperationalError:
        pass
    if not conn.execute("SELECT 1 FROM params WHERE id = 1").fetchone():
        conn.execute("INSERT INTO params (id) VALUES (1)")
    conn.commit()
    conn.close()


def get_params(conn):
    return dict(conn.execute("SELECT * FROM params WHERE id = 1").fetchone())


def get_measurements(conn, seg_id):
    rows = conn.execute(
        "SELECT * FROM measurements WHERE segment_id = ? ORDER BY seq",
        (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_actions(conn, seg_id):
    rows = conn.execute(
        "SELECT * FROM actions WHERE segment_id = ? ORDER BY frame_index",
        (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_edge_observations(conn, seg_id):
    rows = conn.execute(
        "SELECT * FROM edge_observations WHERE segment_id = ?"
        " ORDER BY frame_index, side, kind",
        (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_keyframes(conn, seg_id):
    rows = conn.execute(
        "SELECT * FROM gate_keyframes WHERE segment_id = ?"
        " ORDER BY frame_index", (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_heights(conn, seg_id):
    rows = conn.execute(
        "SELECT * FROM height_observations WHERE segment_id = ?"
        " ORDER BY frame_index, pos", (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_focus_anchors(conn, seg_id):
    rows = conn.execute(
        "SELECT * FROM focus_anchors WHERE segment_id = ?"
        " ORDER BY frame_index", (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def focus_frame_count(frames, edges, heights):
    """焦面工作区帧范围：画格帧、双边观测、测高帧的并集。"""
    n = gate_frame_count(frames, edges)
    if heights:
        n = max(n, max(h["frame_index"] for h in heights) + 1)
    return n


def gate_frame_count(frames, edges):
    """门位工作区帧范围：画格帧与双边观测帧的并集。"""
    n = len(frames)
    if edges:
        n = max(n, max(e["frame_index"] for e in edges) + 1)
    return max(n, 1)


def compute_segment(conn, seg_id):
    """共享计算：复算 JSON、偏移 SVG、稳定轨迹 SVG、走带卡都走这里。

    走带复算与双边门位共用同一份参数版本与双边观测，
    门位区间缓存放 segments.gate_cache，与走带缓存同样按版本失效。
    """
    params = get_params(conn)
    seg = conn.execute("SELECT * FROM segments WHERE id = ?",
                       (seg_id,)).fetchone()
    if not seg:
        return None
    meas = get_measurements(conn, seg_id)
    acts = get_actions(conn, seg_id)
    edges = get_edge_observations(conn, seg_id)
    keyframes = get_keyframes(conn, seg_id)
    heights = get_heights(conn, seg_id)
    focus_anchors = get_focus_anchors(conn, seg_id)
    strategy = seg["focus_strategy"] or "recommended"
    if strategy not in compute.FOCUS_STRATEGIES:
        strategy = "recommended"
    # 缓存前缀只在同一参数版本下有效；版本不同则整段按当前参数重算，
    # 保证同一结果是单一计算基准，不拼合两个版本的帧。
    prefix = None
    if seg["compute_cache"]:
        cache = json.loads(seg["compute_cache"])
        if (isinstance(cache, dict)
                and cache.get("version") == params["version"]):
            prefix = cache["frames"]
    frames, first_slip = compute.build_frames(
        params, meas, acts, anchor_id=seg["anchor_id"], prefix=prefix)
    errors = compute.validate_lock(params, meas, acts)

    gate_cache = json.loads(seg["gate_cache"]) if seg["gate_cache"] else None
    gate = compute.build_gate(
        params, edges, keyframes,
        gate_frame_count(frames, edges), cache=gate_cache)
    gate_errors = compute.validate_gate(params, edges, keyframes, gate)

    # 焦面排程：与门位同一份参数版本、同一校正时间尺；条带圈记的接片
    # 所在帧号一并传给接片基准断裂校验。
    nf = focus_frame_count(frames, edges, heights)
    splice_frames = []
    if frames:
        for sp in compute.markers(meas, "splice"):
            idx = min(range(len(frames)),
                      key=lambda i: abs(frames[i]["x"] - sp["x"]))
            splice_frames.append(idx)
        splice_frames = sorted(set(splice_frames))
    focus_cache = json.loads(seg["focus_cache"]) \
        if seg["focus_cache"] else None
    focus = compute.build_focus(
        params, heights, focus_anchors, frames, strategy,
        cache=focus_cache, n_frames=nf)
    focus_errors = compute.validate_focus(
        params, heights, focus_anchors, frames, focus,
        splice_frames=splice_frames, n_frames=nf)
    errors.extend(gate_errors)
    errors.extend(focus_errors)
    return {
        "segment": dict(seg),
        "params": params,
        "version": params["version"],
        "frames": frames,
        "first_slip": first_slip,
        "lock_errors": errors,
        "measurements": meas,
        "actions": acts,
        "edge_observations": edges,
        "gate_keyframes": keyframes,
        "gate": gate,
        "gate_errors": gate_errors,
        "height_observations": heights,
        "focus_anchors": focus_anchors,
        "focus": focus,
        "focus_errors": focus_errors,
    }


def save_gate_cache(conn, seg_id, gate):
    """只持久化门位区间缓存（参数版本 + 观测指纹 + 端点签名命中即复用）。"""
    cache = {"version": gate["version"], "obs_sig": gate["obs_sig"],
             "zones": gate["cache"]["zones"]}
    conn.execute("UPDATE segments SET gate_cache = ? WHERE id = ?",
                 (json.dumps(cache, ensure_ascii=False), seg_id))


def save_focus_cache(conn, seg_id, focus):
    """只持久化焦面区间缓存（参数版本 + 测高稿指纹 + 策略 + 锚点签名）。"""
    cache = {"version": focus["version"], "h_sig": focus["h_sig"],
             "strategy": focus["strategy"],
             "zones": focus["cache"]["zones"]}
    conn.execute("UPDATE segments SET focus_cache = ? WHERE id = ?",
                 (json.dumps(cache, ensure_ascii=False), seg_id))


def offset_svg(result):
    """偏移曲线 SVG，与 JSON 同源。"""
    frames = result["frames"]
    w, h, pad = 720, 220, 30
    if not frames:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
                '<text x="20" y="40">无帧数据</text></svg>' % (w, h))
    lim = result["params"]["traction_limit"]
    xs = [f["frame"] for f in frames]
    ys = [f["offset"] for f in frames]
    ymax = max([lim * 1.2] + [abs(y) for y in ys])

    def px(i):
        return pad + (w - 2 * pad) * (i - xs[0]) / max(1, xs[-1] - xs[0])

    def py(v):
        return h / 2 - (h / 2 - pad) * v / ymax

    pts = " ".join("%.1f,%.1f" % (px(i), py(v)) for i, v in zip(xs, ys))
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'font-family="monospace" font-size="11">' % (w, h),
        '<rect width="%d" height="%d" fill="#141414"/>' % (w, h),
        # 牵引上限带
        '<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="#3a1d1d"/>'
        % (pad, py(ymax), w - 2 * pad, py(lim) - py(ymax)),
        '<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="#3a1d1d"/>'
        % (pad, py(-lim), w - 2 * pad, py(-ymax) - py(-lim)),
        '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#555"/>'
        % (pad, py(0), w - pad, py(0)),
        '<polyline points="%s" fill="none" stroke="#e8b13c" '
        'stroke-width="1.5"/>' % pts,
    ]
    slip = result["first_slip"]
    if slip:
        parts.append('<circle cx="%.1f" cy="%.1f" r="5" fill="none" '
                     'stroke="#ff5544" stroke-width="2"/>'
                     % (px(slip["frame"]), py(slip["offset"])))
    parts.append('<text x="%d" y="16" fill="#999">参数版本 v%d · 牵引上限 '
                 '±%.2fmm</text>' % (pad, result["version"], lim))
    parts.append("</svg>")
    return "".join(parts)


def gate_svg(result):
    """双边门位稳定轨迹 SVG，与复算 JSON、走带卡同源。

    上半部：中心线横移（实测点 + 补偿后残差）；下半部：画格旋角。
    """
    gate = result["gate"]
    frames = gate["frames"]
    w, h, pad = 720, 260, 34
    if not frames:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
                '<text x="20" y="40">无门位数据</text></svg>' % (w, h))
    xs = [f["frame"] for f in frames]
    x0, x1 = xs[0], xs[-1]

    def px(i):
        return pad + (w - 2 * pad) * (i - x0) / max(1, x1 - x0)

    def make_py(vmax, mid):
        def py(v):
            return mid - (mid - 18) * (v / vmax) if vmax else mid
        return py

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'font-family="monospace" font-size="11">' % (w, h),
        '<rect width="%d" height="%d" fill="#141414"/>' % (w, h),
    ]

    # 横移面板
    mid_y = 88
    cs = [f["meas_center"] for f in frames if f["meas_center"] is not None]
    cmax = max([0.5] + [abs(v) for v in cs])
    pyc = make_py(cmax * 1.15, mid_y)
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#555"/>'
                 % (pad, pyc(0), w - pad, pyc(0)))
    meas_pts = " ".join("%.1f,%.1f" % (px(f["frame"]), pyc(f["meas_center"]))
                        for f in frames if f["meas_center"] is not None)
    if meas_pts:
        parts.append('<polyline points="%s" fill="none" stroke="#7fb8ff" '
                     'stroke-width="1.4"/>' % meas_pts)
    # 补偿后残差 = 实测横移 − 补偿 − 窗口偏移
    woff = result["params"]["window_offset"]
    res_pts = " ".join(
        "%.1f,%.1f" % (px(f["frame"]),
                       pyc((f["meas_center"] or 0.0) - f["shift"] - woff))
        for f in frames if f["meas_center"] is not None)
    if res_pts:
        parts.append('<polyline points="%s" fill="none" stroke="#7fd07f" '
                     'stroke-width="1.6"/>' % res_pts)
    # 补偿轨迹本身
    comp_pts = " ".join("%.1f,%.1f" % (px(f["frame"]), pyc(f["shift"]))
                        for f in frames)
    parts.append('<polyline points="%s" fill="none" stroke="#e8b13c" '
                 'stroke-width="1.2" stroke-dasharray="4 2"/>' % comp_pts)
    for kf in gate["keyframes"]:
        parts.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="#e8b13c"/>'
                     % (px(kf["frame_index"]), pyc(kf["shift"])))
    parts.append('<text x="%d" y="14" fill="#9cf">实测横移</text>'
                 '<text x="100" y="14" fill="#e8b13c">门位补偿</text>'
                 '<text x="180" y="14" fill="#7fd07f">补偿残差</text>' % pad)

    # 旋角面板
    ang_mid = 196
    amax = max([0.5] + [abs(f["angle"]) for f in frames])
    ameas = max([0.5] + [abs(f["meas_angle"]) for f in frames
                         if f["meas_angle"] is not None])
    amax = max(amax, ameas)
    pya = make_py(amax * 1.15, ang_mid)
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#555"/>'
                 % (pad, pya(0), w - pad, pya(0)))
    ang_pts = " ".join("%.1f,%.1f" % (px(f["frame"]), pya(f["angle"]))
                       for f in frames)
    parts.append('<polyline points="%s" fill="none" stroke="#e8b13c" '
                 'stroke-width="1.4"/>' % ang_pts)
    ma_pts = " ".join("%.1f,%.1f" % (px(f["frame"]), pya(f["meas_angle"]))
                      for f in frames if f["meas_angle"] is not None)
    if ma_pts:
        parts.append('<polyline points="%s" fill="none" stroke="#7fb8ff" '
                     'stroke-width="1.2" stroke-dasharray="3 2"/>' % ma_pts)
    parts.append('<text x="%d" y="120" fill="#999">中心线横移 mm</text>'
                 '<text x="%d" y="128" fill="#9cf">— 实测旋角</text>'
                 '<text x="%d" y="136" fill="#e8b13c">— 补偿旋角</text>'
                 % (pad, pad, pad))

    # 校验问题首帧钉红
    if result["gate_errors"]:
        first = None
        for token in result["gate_errors"][0].split():
            if token.isdigit():
                first = int(token)
                break
        if first is not None:
            parts.append('<line x1="%.1f" y1="20" x2="%.1f" y2="%d" '
                         'stroke="#ff5544" stroke-width="2"/>'
                         % (px(first), px(first), h - 24))
    parts.append('<text x="%d" y="%d" fill="#999">参数版本 v%d · 观测 %s · '
                 '配对面 %d · 关键帧 %d</text>'
                 % (pad, h - 8, result["version"], gate["obs_sig"],
                    gate["pair_count"], len(gate["keyframes"])))
    parts.append("</svg>")
    return "".join(parts)


def focus_svg(result):
    """焦域热图 SVG，与重演 JSON、走带卡同源。

    主面板：纵轴物距（翘曲包络 z_near–z_far 灰色带，测点位置蓝点），
    下达焦位金线，景深窗 [focus±dof/2] 金色半透明带；
    下面板：逐帧清晰覆盖比例（≥下限绿色，不足红色），空齿帧灰色缺口。
    """
    focus = result["focus"]
    rows = focus["frames"]
    params = result["params"]
    dof = params["lens_dof"]
    w, h, pad = 720, 280, 34
    if not rows:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
                '<text x="20" y="40">无焦面数据</text></svg>' % (w, h))
    xs = [r["frame"] for r in rows]
    x0, x1 = xs[0], xs[-1]

    def px(i):
        return pad + (w - 2 * pad) * (i - x0) / max(1, x1 - x0)

    zvals = [v for r in rows for v in (r["z_near"], r["z_far"], r["focus"])
             if v is not None]
    if not zvals:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
                '<text x="20" y="40">无测高点</text></svg>' % (w, h))
    zlo, zhi = min(zvals) - dof, max(zvals) + dof
    top0, bot0 = 24.0, 176.0

    def pz(z):
        return top0 + (bot0 - top0) * (z - zlo) / max(1e-9, zhi - zlo)

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'font-family="monospace" font-size="11">' % (w, h),
        '<rect width="%d" height="%d" fill="#141414"/>' % (w, h),
    ]

    # 调焦范围限位线
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#553"/>'
                 % (pad, pz(params["focus_near"]), w - pad,
                    pz(params["focus_near"])))
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#553"/>'
                 % (pad, pz(params["focus_far"]), w - pad,
                    pz(params["focus_far"])))

    # 翘曲包络带 + 测点
    near_pts, far_pts = [], []
    bw = max(2.0, (w - 2 * pad) / max(1, x1 - x0) - 1.0)
    for r in rows:
        if r["z_near"] is None:
            continue
        xn, ynear, yfar = px(r["frame"]), pz(r["z_near"]), pz(r["z_far"])
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
                     'stroke="#5a6a8a" stroke-width="%.1f"/>'
                     % (xn, ynear, xn, yfar, bw))
        near_pts.append((xn, ynear))
        far_pts.append((xn, yfar))
    npoly = " ".join("%.1f,%.1f" % p for p in near_pts)
    fpoly = " ".join("%.1f,%.1f" % p for p in far_pts)
    if npoly:
        parts.append('<polyline points="%s" fill="none" stroke="#7fb8ff" '
                     'stroke-width="1.2"/>' % npoly)
        parts.append('<polyline points="%s" fill="none" stroke="#7fb8ff" '
                     'stroke-width="1.2"/>' % fpoly)

    # 各标准测点（淡蓝小点）
    for r in rows:
        bp = r.get("by_pos")
        if not bp:
            continue
        for pos in compute.FOCUS_CANON:
            z = bp.get(pos)
            if z is not None:
                parts.append('<circle cx="%.1f" cy="%.1f" r="1.6" '
                             'fill="#9cc8ff"/>' % (px(r["frame"]), pz(z)))

    # 景深窗带（按下达焦位）与焦位轨迹
    bar_w = max(3.0, (w - 2 * pad) / max(1, x1 - x0) - 1.0)
    for r in rows:
        if r["focus"] is None or r["skipped"]:
            continue
        ytop = pz(r["focus"] + dof / 2.0)
        ybot = pz(r["focus"] - dof / 2.0)
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'fill="#e8b13c" opacity="0.16"/>'
                     % (px(r["frame"]) - bar_w / 2.0, ytop,
                        bar_w, ybot - ytop))
    fpts = " ".join("%.1f,%.1f" % (px(r["frame"]), pz(r["focus"]))
                    for r in rows if r["focus"] is not None
                    and not r["skipped"])
    if fpts:
        parts.append('<polyline points="%s" fill="none" stroke="#e8b13c" '
                     'stroke-width="1.8"/>' % fpts)
    # 推荐目标（被夹住/机构跟不动时可能与实际不同）：虚金线
    tpts = " ".join("%.1f,%.1f" % (px(r["frame"]), pz(r["target"]))
                    for r in rows if r.get("target") is not None
                    and not r["skipped"]
                    and abs(r["target"] - r["focus"]) > 1e-9)
    if tpts:
        parts.append('<polyline points="%s" fill="none" stroke="#ffd766" '
                     'stroke-width="1.0" stroke-dasharray="3 3"/>' % tpts)
    # 人工锚点
    for a in focus["anchors"]:
        parts.append('<circle cx="%.1f" cy="%.1f" r="3.6" fill="#7fd07f"/>'
                     % (px(a["frame_index"]), pz(a["focus"])))

    parts.append('<text x="%d" y="14" fill="#9cf">翘曲包络/测点</text>'
                 '<text x="150" y="14" fill="#e8b13c">下达焦位/景深窗</text>'
                 '<text x="300" y="14" fill="#7fd07f">焦点锚点</text>' % pad)

    # 覆盖比例面板
    cov_mid = 232.0
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#555"/>'
                 % (pad, cov_mid, w - pad, cov_mid))
    bar_h = 30.0
    for r in rows:
        c = r["coverage"]
        x = px(r["frame"])
        if c is None:
            color = "#333"   # 空齿/无包络
            hgt = bar_h
        elif c >= compute.MIN_FOCUS_COVERAGE:
            color, hgt = "#7fd07f", bar_h * c
        else:
            color, hgt = "#ff5544", bar_h * c
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'fill="%s"/>'
                     % (x - 1.5, cov_mid - hgt,
                        max(2.0, (w - 2 * pad) / max(1, x1 - x0) - 1.0),
                        hgt, color))
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                 'stroke="#ffaa33" stroke-dasharray="4 3"/>'
                 % (pad, cov_mid - bar_h * compute.MIN_FOCUS_COVERAGE,
                    w - pad, cov_mid - bar_h * compute.MIN_FOCUS_COVERAGE))
    parts.append('<text x="%d" y="206" fill="#999">物距 mm · 策略 %s</text>'
                 '<text x="%d" y="%d" fill="#999">清晰覆盖比例（下限 '
                 '%.0f%%）</text>'
                 % (pad, compute.FOCUS_STRATEGY_CN[focus["strategy"]],
                    pad, cov_mid + 28,
                    100 * compute.MIN_FOCUS_COVERAGE))

    # 校验问题首帧钉红
    if result["focus_errors"]:
        first = None
        for token in result["focus_errors"][0].split():
            if token.isdigit():
                first = int(token)
                break
        if first is not None and x0 <= first <= x1:
            parts.append('<line x1="%.1f" y1="20" x2="%.1f" y2="%d" '
                         'stroke="#ff5544" stroke-width="2"/>'
                         % (px(first), px(first), h - 24))
    parts.append('<text x="%d" y="%d" fill="#999">参数版本 v%d · 测高稿 %s '
                 '· 景深 %.2fmm · 电机 %.1fmm/s</text>'
                 % (pad, h - 8, result["version"], focus["h_sig"],
                    dof, params["motor_speed"]))
    parts.append("</svg>")
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    server_version = "FilmPath/1.0"

    # ---------- 基础 ----------

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, fmt, *args):
        pass

    # ---------- 路由 ----------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path.rsplit("/", 1)[-1]
            ctype = ("text/javascript" if name.endswith(".js")
                     else "text/css" if name.endswith(".css")
                     else "application/octet-stream")
            return self._file(name, ctype)

        conn = db()
        try:
            if path == "/api/params":
                return self._json(get_params(conn))
            if path == "/api/segments":
                rows = conn.execute(
                    "SELECT * FROM segments ORDER BY id").fetchall()
                return self._json([dict(r) for r in rows])
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "segments"]:
                result = compute_segment(conn, int(parts[2]))
                return self._json(result or {"error": "not found"},
                                  200 if result else 404)
            if len(parts) == 4 and parts[:2] == ["api", "segments"]:
                seg_id = int(parts[2])
                if parts[3] == "offset.svg":
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    return self._send(200, offset_svg(result), "image/svg+xml")
                if parts[3] == "gate.svg":
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    return self._send(200, gate_svg(result), "image/svg+xml")
                if parts[3] == "focus.svg":
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    return self._send(200, focus_svg(result), "image/svg+xml")
                if parts[3] == "focus_replay":
                    # 重演 JSON：与焦域热图、走带卡固定同一测高稿与策略版本
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    f = result["focus"]
                    return self._json({
                        "version": result["version"],
                        "h_sig": f["h_sig"],
                        "strategy": f["strategy"],
                        "lens": {
                            "dof": result["params"]["lens_dof"],
                            "focus_near": result["params"]["focus_near"],
                            "focus_far": result["params"]["focus_far"],
                            "motor_speed": result["params"]["motor_speed"],
                            "settle_time": result["params"]["settle_time"]},
                        "anchors": f["anchors"],
                        "ambiguous_frames": f["ambiguous_frames"],
                        "errors": result["focus_errors"],
                        "frames": [{
                            "frame": r["frame"],
                            "tc": next((x["tc"] for x in result["frames"]
                                        if x["frame"] == r["frame"]), None),
                            "z_near": r["z_near"], "z_far": r["z_far"],
                            "target": r["target"], "focus": r["focus"],
                            "speed": (None if r["speed"] == float("inf")
                                      or r["speed"] == float("-inf")
                                      else r["speed"]),
                            "dt": r["dt"], "settled": r["settled"],
                            "in_range": r["in_range"],
                            "coverage": r["coverage"],
                            "skipped": r["skipped"],
                        } for r in f["frames"]],
                    })
                if parts[3] == "transport_card":
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    return self._json({
                        "version": result["version"],
                        "obs_sig": result["gate"]["obs_sig"],
                        "h_sig": result["focus"]["h_sig"],
                        "focus_strategy": result["focus"]["strategy"],
                        "segment": result["segment"]["name"],
                        "locked": bool(result["segment"]["locked"]),
                        "frames": len(result["frames"]),
                        "first_slip": result["first_slip"],
                        "lock_errors": result["lock_errors"],
                        "gate": {
                            "pair_count": result["gate"]["pair_count"],
                            "keyframes": len(result["gate"]["keyframes"]),
                            "gate_frames": len(result["gate"]["frames"]),
                            "errors": result["gate_errors"],
                        },
                        "focus": {
                            "height_points": len(
                                [h for h in result["height_observations"]
                                 if h.get("usable", 1)]),
                            "anchors": len(result["focus"]["anchors"]),
                            "focus_frames": len(result["focus"]["frames"]),
                            "min_coverage": min(
                                (r["coverage"] for r in result["focus"]["frames"]
                                 if r["coverage"] is not None),
                                default=None),
                            "errors": result["focus_errors"],
                        },
                    })
        finally:
            conn.close()
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        conn = db()
        try:
            if path == "/api/params":
                fields = ("film_width", "nominal_pitch", "window_offset",
                          "window_size", "traction_limit", "safe_margin",
                          "lens_dof", "focus_near", "focus_far",
                          "motor_speed", "settle_time")
                cur_params = get_params(conn)
                sets = ", ".join("%s = ?" % f for f in fields)
                vals = [float(body[f]) if f in body else cur_params[f]
                        for f in fields]
                conn.execute(
                    "UPDATE params SET %s, version = version + 1 WHERE id = 1"
                    % sets, vals)
                conn.commit()
                return self._json(get_params(conn))

            if path == "/api/segments":
                cur = conn.execute("INSERT INTO segments (name) VALUES (?)",
                                   (body.get("name") or "未命名段",))
                conn.commit()
                return self._json({"id": cur.lastrowid}, 201)

            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "segments"]:
                seg_id = int(parts[2])
                seg = conn.execute("SELECT * FROM segments WHERE id = ?",
                                   (seg_id,)).fetchone()
                if not seg:
                    return self._json({"error": "not found"}, 404)
                sub = parts[3] if len(parts) > 3 else ""

                # 已锁区间只读：除解锁/锚点外一律拒绝
                if seg["locked"] and sub not in ("unlock", "anchor"):
                    return self._json({"error": "段已锁定，只读"}, 409)

                if sub == "measurements":
                    cur = conn.execute(
                        "INSERT INTO measurements (segment_id, seq, x, y, kind)"
                        " VALUES (?,?,?,?,?)",
                        (seg_id, int(body["seq"]), float(body["x"]),
                         float(body.get("y", 0)), body.get("kind", "sprocket")))
                    conn.commit()
                    return self._json({"id": cur.lastrowid}, 201)

                if sub == "actions":
                    cur = conn.execute(
                        "INSERT INTO actions (segment_id, frame_index, type,"
                        " factor, span, resume_tc) VALUES (?,?,?,?,?,?)",
                        (seg_id, int(body["frame_index"]), body["type"],
                         body.get("factor"), int(body.get("span", 1)),
                         body.get("resume_tc")))
                    conn.commit()
                    return self._json({"id": cur.lastrowid}, 201)

                # ---- 双边门位稳定工作区 ----

                if (len(parts) == 5 and parts[3] == "edges"):
                    e_id = int(parts[4])
                    obs = conn.execute(
                        "SELECT * FROM edge_observations WHERE id = ?"
                        " AND segment_id = ?", (e_id, seg_id)).fetchone()
                    if not obs:
                        return self._json({"error": "not found"}, 404)
                    vals = (body.get("x", obs["x"]), body.get("y", obs["y"]),
                            1 if body.get("usable", obs["usable"]) else 0, e_id)
                    conn.execute(
                        "UPDATE edge_observations SET x = ?, y = ?, usable = ?"
                        " WHERE id = ?", vals)
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_gate_cache(conn, seg_id, result["gate"])
                    conn.commit()
                    return self._json(result["gate"])

                if (len(parts) == 5 and parts[3] == "keyframes"):
                    kf_id = int(parts[4])
                    kf = conn.execute(
                        "SELECT * FROM gate_keyframes WHERE id = ?"
                        " AND segment_id = ?", (kf_id, seg_id)).fetchone()
                    if not kf:
                        return self._json({"error": "not found"}, 404)
                    shift = float(body["shift"]) if "shift" in body \
                        else kf["shift"]
                    angle = float(body["angle"]) if "angle" in body \
                        else kf["angle"]
                    conn.execute(
                        "UPDATE gate_keyframes SET shift = ?, angle = ?"
                        " WHERE id = ?", (shift, angle, kf_id))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    # 调整某个关键帧：只相邻区间重算，结果里带回重算区间
                    save_gate_cache(conn, seg_id, result["gate"])
                    conn.commit()
                    return self._json(result["gate"])

                # ---- 焦面排程 ----

                if (len(parts) == 5 and parts[3] == "heights"):
                    h_id = int(parts[4])
                    obs = conn.execute(
                        "SELECT * FROM height_observations WHERE id = ?"
                        " AND segment_id = ?", (h_id, seg_id)).fetchone()
                    if not obs:
                        return self._json({"error": "not found"}, 404)
                    zval = body.get("z", obs["z"])
                    conn.execute(
                        "UPDATE height_observations SET z = ?, usable = ?"
                        " WHERE id = ?",
                        (zval,
                         1 if body.get("usable", obs["usable"]) else 0,
                         h_id))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_focus_cache(conn, seg_id, result["focus"])
                    conn.commit()
                    return self._json(result["focus"])

                if (len(parts) == 5 and parts[3] == "focus_anchors"):
                    fa_id = int(parts[4])
                    fa = conn.execute(
                        "SELECT * FROM focus_anchors WHERE id = ?"
                        " AND segment_id = ?", (fa_id, seg_id)).fetchone()
                    if not fa:
                        return self._json({"error": "not found"}, 404)
                    focus = float(body["focus"]) if "focus" in body \
                        else fa["focus"]
                    conn.execute(
                        "UPDATE focus_anchors SET focus = ? WHERE id = ?",
                        (focus, fa_id))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    # 编辑锚点仅更新夹在相邻锚点间的结果（区间缓存）
                    save_focus_cache(conn, seg_id, result["focus"])
                    conn.commit()
                    return self._json(result["focus"])

                if sub == "heights":
                    # Canvas 画格测高：frame_index + pos(C/TL/TR/BL/BR/SB/SA)
                    # + z 物距；usable=0 表示该点废读（不参与拟合）
                    pos = body.get("pos")
                    if pos not in (compute.FOCUS_CANON
                                   + (compute.FOCUS_SPLICE_PRE,
                                      compute.FOCUS_SPLICE_POST)):
                        return self._json(
                            {"error": "pos 须为 C/TL/TR/BL/BR/SB/SA"}, 400)
                    cur = conn.execute(
                        "INSERT INTO height_observations (segment_id,"
                        " frame_index, pos, z, usable) VALUES (?,?,?,?,?)",
                        (seg_id, int(body["frame_index"]), pos,
                         body.get("z"),
                         1 if body.get("usable", True) else 0))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_focus_cache(conn, seg_id, result["focus"])
                    conn.commit()
                    return self._json({"id": cur.lastrowid,
                                       "focus": result["focus"]}, 201)

                if sub == "focus_anchors":
                    fi = int(body["frame_index"])
                    dup = conn.execute(
                        "SELECT 1 FROM focus_anchors WHERE segment_id = ?"
                        " AND frame_index = ?", (seg_id, fi)).fetchone()
                    if dup:
                        return self._json(
                            {"error": "帧 %d 已有焦点锚点，请拖动或删除后重建"
                             % fi}, 400)
                    if "focus" in body:
                        val = float(body["focus"])
                    else:
                        dk = compute.default_focus_anchor(
                            get_params(conn), get_heights(conn, seg_id), fi)
                        val = dk["focus"]
                    cur = conn.execute(
                        "INSERT INTO focus_anchors (segment_id, frame_index,"
                        " focus) VALUES (?,?,?)", (seg_id, fi, val))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_focus_cache(conn, seg_id, result["focus"])
                    conn.commit()
                    return self._json({"id": cur.lastrowid,
                                       "focus": result["focus"]}, 201)

                if sub == "focus_strategy":
                    strat = body.get("strategy")
                    if strat not in compute.FOCUS_STRATEGIES:
                        return self._json(
                            {"error": "strategy 须为 constant/recommended/manual"},
                            400)
                    conn.execute(
                        "UPDATE segments SET focus_strategy = ? WHERE id = ?",
                        (strat, seg_id))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_focus_cache(conn, seg_id, result["focus"])
                    conn.commit()
                    return self._json({"strategy": strat,
                                       "focus": result["focus"]})

                if sub == "edges":
                    # 逐帧标记左/右齿孔中心、片边、不可用缺口（usable=0）
                    side = body.get("side")
                    kind = body.get("kind")
                    if side not in ("L", "R") or kind not in (
                            "sprocket", "edge"):
                        return self._json(
                            {"error": "side 须为 L/R，kind 须为 sprocket/edge"},
                            400)
                    cur = conn.execute(
                        "INSERT INTO edge_observations (segment_id,"
                        " frame_index, side, kind, x, y, usable)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (seg_id, int(body["frame_index"]), side, kind,
                         body.get("x"), body.get("y"),
                         1 if body.get("usable", True) else 0))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_gate_cache(conn, seg_id, result["gate"])
                    conn.commit()
                    return self._json({"id": cur.lastrowid,
                                       "gate": result["gate"]}, 201)

                if sub == "keyframes":
                    fi = int(body["frame_index"])
                    dup = conn.execute(
                        "SELECT 1 FROM gate_keyframes WHERE segment_id = ?"
                        " AND frame_index = ?", (seg_id, fi)).fetchone()
                    if dup:
                        return self._json(
                            {"error": "帧 %d 已有关键帧，请拖放或删除后重建"
                             % fi}, 400)
                    # 默认值贴住该帧实测门位；显式给值则用给值
                    params = get_params(conn)
                    edges = get_edge_observations(conn, seg_id)
                    if "shift" in body or "angle" in body:
                        shift = float(body.get("shift", 0.0))
                        angle = float(body.get("angle", 0.0))
                    else:
                        dk = compute.default_keyframe(params, edges, fi)
                        shift, angle = dk["shift"], dk["angle"]
                    cur = conn.execute(
                        "INSERT INTO gate_keyframes (segment_id, frame_index,"
                        " shift, angle) VALUES (?,?,?,?)",
                        (seg_id, fi, shift, angle))
                    conn.commit()
                    result = compute_segment(conn, seg_id)
                    save_gate_cache(conn, seg_id, result["gate"])
                    conn.commit()
                    return self._json({"id": cur.lastrowid,
                                       "gate": result["gate"]}, 201)

                if sub == "lock":
                    result = compute_segment(conn, seg_id)
                    if result["lock_errors"]:
                        return self._json({"locked": False,
                                           "errors": result["lock_errors"]},
                                          422)
                    cache = json.dumps({"version": result["version"],
                                        "frames": result["frames"]})
                    conn.execute(
                        "UPDATE segments SET locked = 1, compute_cache = ?"
                        " WHERE id = ?", (cache, seg_id))
                    conn.commit()
                    return self._json({"locked": True})

                if sub == "unlock":
                    conn.execute("UPDATE segments SET locked = 0 WHERE id = ?",
                                 (seg_id,))
                    conn.commit()
                    return self._json({"locked": False})

                if sub == "anchor":
                    # 改锚点只续算后方：参数版本未变时锚点前沿用缓存帧，
                    # 版本已变则整段按当前参数重算（单一基准）。
                    mid = body.get("measurement_id")
                    conn.execute(
                        "UPDATE segments SET anchor_id = ? WHERE id = ?",
                        (int(mid) if mid is not None else None, seg_id))
                    result = compute_segment(conn, seg_id)
                    conn.execute(
                        "UPDATE segments SET compute_cache = ? WHERE id = ?",
                        (json.dumps({"version": result["version"],
                                     "frames": result["frames"]}), seg_id))
                    conn.commit()
                    return self._json(result)
        finally:
            conn.close()
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        conn = db()
        try:
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "measurements":
                seg = conn.execute(
                    "SELECT s.locked FROM segments s JOIN measurements m"
                    " ON m.segment_id = s.id WHERE m.id = ?",
                    (int(parts[2]),)).fetchone()
                if seg and seg["locked"]:
                    return self._json({"error": "段已锁定，只读"}, 409)
                conn.execute("DELETE FROM measurements WHERE id = ?",
                             (int(parts[2]),))
                conn.commit()
                return self._json({"deleted": True})
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "actions":
                seg = conn.execute(
                    "SELECT s.locked FROM segments s JOIN actions a"
                    " ON a.segment_id = s.id WHERE a.id = ?",
                    (int(parts[2]),)).fetchone()
                if seg and seg["locked"]:
                    return self._json({"error": "段已锁定，只读"}, 409)
                conn.execute("DELETE FROM actions WHERE id = ?",
                             (int(parts[2]),))
                conn.commit()
                return self._json({"deleted": True})
            if (len(parts) == 3 and parts[0] == "api"
                    and parts[1] in ("edges", "keyframes")):
                table = ("edge_observations" if parts[1] == "edges"
                         else "gate_keyframes")
                seg = conn.execute(
                    "SELECT s.locked FROM segments s JOIN %s t"
                    " ON t.segment_id = s.id WHERE t.id = ?" % table,
                    (int(parts[2]),)).fetchone()
                if not seg:
                    return self._json({"error": "not found"}, 404)
                if seg["locked"]:
                    return self._json({"error": "段已锁定，只读"}, 409)
                conn.execute("DELETE FROM %s WHERE id = ?" % table,
                             (int(parts[2]),))
                conn.commit()
                return self._json({"deleted": True})
            if (len(parts) == 3 and parts[0] == "api"
                    and parts[1] in ("heights", "focus_anchors")):
                table = ("height_observations" if parts[1] == "heights"
                         else "focus_anchors")
                seg = conn.execute(
                    "SELECT s.locked FROM segments s JOIN %s t"
                    " ON t.segment_id = s.id WHERE t.id = ?" % table,
                    (int(parts[2]),)).fetchone()
                if not seg:
                    return self._json({"error": "not found"}, 404)
                if seg["locked"]:
                    return self._json({"error": "段已锁定，只读"}, 409)
                conn.execute("DELETE FROM %s WHERE id = ?" % table,
                             (int(parts[2]),))
                conn.commit()
                return self._json({"deleted": True})
        finally:
            conn.close()
        self._json({"error": "not found"}, 404)

    def _file(self, name, ctype):
        full = os.path.join(STATIC, name)
        if not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


if __name__ == "__main__":
    import sys
    init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print("走带编排服务 http://127.0.0.1:%d" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
