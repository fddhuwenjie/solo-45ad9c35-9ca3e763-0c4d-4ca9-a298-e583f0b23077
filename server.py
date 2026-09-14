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
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    anchor_id INTEGER,
    compute_cache TEXT
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
    errors.extend(gate_errors)
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
    }


def save_gate_cache(conn, seg_id, gate):
    """只持久化门位区间缓存（参数版本 + 观测指纹 + 端点签名命中即复用）。"""
    cache = {"version": gate["version"], "obs_sig": gate["obs_sig"],
             "zones": gate["cache"]["zones"]}
    conn.execute("UPDATE segments SET gate_cache = ? WHERE id = ?",
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
                if parts[3] == "transport_card":
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    return self._json({
                        "version": result["version"],
                        "obs_sig": result["gate"]["obs_sig"],
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
                          "window_size", "traction_limit", "safe_margin")
                sets = ", ".join("%s = ?" % f for f in fields)
                vals = [float(body[f]) for f in fields]
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
