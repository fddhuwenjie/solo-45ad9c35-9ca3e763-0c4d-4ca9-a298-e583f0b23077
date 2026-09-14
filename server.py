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


def compute_segment(conn, seg_id):
    """共享计算：JSON、SVG、走带卡都走这里。"""
    params = get_params(conn)
    seg = conn.execute("SELECT * FROM segments WHERE id = ?",
                       (seg_id,)).fetchone()
    if not seg:
        return None
    meas = get_measurements(conn, seg_id)
    acts = get_actions(conn, seg_id)
    prefix = json.loads(seg["compute_cache"]) if seg["compute_cache"] else None
    frames, first_slip = compute.build_frames(
        params, meas, acts, anchor_id=seg["anchor_id"], prefix=prefix)
    errors = compute.validate_lock(params, meas, acts)
    return {
        "segment": dict(seg),
        "params": params,
        "version": params["version"],
        "frames": frames,
        "first_slip": first_slip,
        "lock_errors": errors,
        "measurements": meas,
        "actions": acts,
    }


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
                if parts[3] == "transport_card":
                    result = compute_segment(conn, seg_id)
                    if not result:
                        return self._json({"error": "not found"}, 404)
                    return self._json({
                        "version": result["version"],
                        "segment": result["segment"]["name"],
                        "locked": bool(result["segment"]["locked"]),
                        "frames": len(result["frames"]),
                        "first_slip": result["first_slip"],
                        "lock_errors": result["lock_errors"],
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
                          "window_size", "traction_limit")
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

                if sub == "lock":
                    result = compute_segment(conn, seg_id)
                    if result["lock_errors"]:
                        return self._json({"locked": False,
                                           "errors": result["lock_errors"]},
                                          422)
                    cache = json.dumps(result["frames"])
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
                    # 改锚点只续算后方：锚点前沿用缓存帧
                    conn.execute(
                        "UPDATE segments SET anchor_id = ? WHERE id = ?",
                        (int(body["measurement_id"]), seg_id))
                    conn.commit()
                    return self._json(compute_segment(conn, seg_id))
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
                conn.execute("DELETE FROM actions WHERE id = ?",
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
