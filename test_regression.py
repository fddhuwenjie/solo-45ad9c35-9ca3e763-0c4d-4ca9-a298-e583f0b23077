"""走带编排回归测试：标准库 unittest + 临时 SQLite + 本地 HTTP 服务。

    python3 -m unittest test_regression -v

每个用例在临时目录建库（不触碰仓库里的 measurements.db），
HTTP 服务监听 127.0.0.1 的临时端口，用例结束即回收。
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PARAM_FIELDS = ("film_width", "nominal_pitch", "window_offset",
                "window_size", "traction_limit", "safe_margin",
                "lens_dof", "focus_near", "focus_far",
                "motor_speed", "settle_time")


class TransportRegressionTest(unittest.TestCase):
    """HTTP 服务全类共享，SQLite 库每用例独立。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        server.DB = os.path.join(self._tmp.name, "measurements.db")
        server.init_db()

    def tearDown(self):
        self._tmp.cleanup()

    # ---------- 基础工具 ----------

    def api(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data,
            method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get_raw(self, path):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d%s" % (self.port, path)) as resp:
            return (resp.status, resp.read().decode("utf-8"),
                    resp.headers.get("Content-Type"))

    def set_params(self, **over):
        _, cur = self.api("GET", "/api/params")
        body = {k: cur[k] for k in PARAM_FIELDS}
        body.update(over)
        return self.api("POST", "/api/params", body)

    def create_segment(self, name):
        status, body = self.api("POST", "/api/segments", {"name": name})
        self.assertEqual(status, 201)
        return body["id"]

    def add_measurement(self, seg, seq, x, kind="sprocket"):
        status, body = self.api(
            "POST", "/api/segments/%d/measurements" % seg,
            {"seq": seq, "x": x, "y": 0, "kind": kind})
        self.assertEqual(status, 201)
        return body["id"]

    def add_sprockets(self, seg, count, pitch):
        """等距齿孔，返回测量 id 列表；帧 i 的首缘是第 i 个齿孔。"""
        return [self.add_measurement(seg, i + 1, i * pitch)
                for i in range(count)]

    # ---------- 双边门位辅助 ----------

    def add_edge(self, seg, fi, side, kind, x=None, y=None, usable=True):
        status, body = self.api(
            "POST", "/api/segments/%d/edges" % seg,
            {"frame_index": fi, "side": side, "kind": kind,
             "x": x, "y": y, "usable": usable})
        self.assertEqual(status, 201, body)
        return body["id"]

    def add_bilateral(self, seg, n, shift_r=0.0, skew_r=0.0,
                      edge_shift=0.0, edge_skew=0.0):
        """n 帧双侧齿孔+片边。

        右侧齿孔 x 每帧多 skew_r、y 每帧多 shift_r（单边收缩→旋转+横移）；
        片边同理。y 为各侧相对标称位向片心的收缩量。
        """
        pitch = 7.6
        for fi in range(n):
            self.add_edge(seg, fi, "L", "sprocket",
                          x=fi * pitch, y=0.2)
            self.add_edge(seg, fi, "R", "sprocket",
                          x=fi * pitch + skew_r * fi, y=0.2 + shift_r * fi)
            self.add_edge(seg, fi, "L", "edge", x=fi * pitch,
                          y=0.3 + edge_shift)
            self.add_edge(seg, fi, "R", "edge",
                          x=fi * pitch + edge_skew * fi,
                          y=0.3 + edge_shift + shift_r * fi)

    def add_keyframe(self, seg, fi, shift=None, angle=None):
        body = {"frame_index": fi}
        if shift is not None:
            body["shift"] = shift
        if angle is not None:
            body["angle"] = angle
        status, out = self.api(
            "POST", "/api/segments/%d/keyframes" % seg, body)
        self.assertEqual(status, 201, out)
        return out["id"]

    def patch_keyframe(self, seg, kf_id, **fields):
        return self.api("POST", "/api/segments/%d/keyframes/%d"
                        % (seg, kf_id), fields)

    def gate_result(self, seg):
        _, result = self.api("GET", "/api/segments/%d" % seg)
        return result["gate"]

    @staticmethod
    def gate_frame(gate, fi):
        return next(f for f in gate["frames"] if f["frame"] == fi)

    @staticmethod
    def by_frame(result):
        return {f["frame"]: f for f in result["frames"]}

    # ---------- 焦面排程辅助 ----------

    def add_height(self, seg, fi, pos, z, usable=True):
        status, body = self.api(
            "POST", "/api/segments/%d/heights" % seg,
            {"frame_index": fi, "pos": pos, "z": z, "usable": usable})
        self.assertEqual(status, 201, body)
        return body["id"]

    def cover_focus_frames(self, seg, n, zc=50.0, warp=0.0):
        """n 帧五点测高：中央 zc，四角 ±warp（交替布包络）。"""
        for fi in range(n):
            self.add_height(seg, fi, "C", zc)
            for pos, s in (("TL", -1), ("TR", 1), ("BL", 1), ("BR", -1)):
                self.add_height(seg, fi, pos, zc + s * warp)

    def add_focus_anchor(self, seg, fi, focus=None):
        body = {"frame_index": fi}
        if focus is not None:
            body["focus"] = focus
        status, out = self.api(
            "POST", "/api/segments/%d/focus_anchors" % seg, body)
        self.assertEqual(status, 201, out)
        return out["id"]

    def set_focus_strategy(self, seg, strat):
        status, out = self.api(
            "POST", "/api/segments/%d/focus_strategy" % seg,
            {"strategy": strat})
        self.assertEqual(status, 200, out)
        return out

    def focus_result(self, seg):
        _, result = self.api("GET", "/api/segments/%d" % seg)
        return result["focus"]

    @staticmethod
    def focus_row(focus, fi):
        return next(r for r in focus["frames"] if r["frame"] == fi)

    # ---------- 用例 ----------

    def test_first_anchor_after_param_bump(self):
        """参数换版后的首次设锚：整段按 v2 重算，锚点当次生效。"""
        seg = self.create_segment("换版首锚")
        ids = self.add_sprockets(seg, 12, 8.5)   # 帧 0..10，实测节距 8.5

        # v1（默认标称节距 7.62）下锁定，建立 v1 缓存
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200)
        self.assertTrue(body["locked"])
        _, v1 = self.api("GET", "/api/segments/%d" % seg)
        self.assertEqual(v1["version"], 1)
        # v1 缓存里 frame 8 偏移为 8×(8.5−7.62)=7.04；
        # 若换版后误用这份缓存前缀，下面的 4.00 断言会抓到
        self.assertAlmostEqual(v1["frames"][8]["offset"], 7.04, places=6)

        # 升到 v2：标称节距 7.62 -> 8.0
        self.set_params(nominal_pitch=8.0)
        _, params = self.api("GET", "/api/params")
        self.assertEqual(params["version"], 2)

        # 首次把 frame 9 设为锚点（锁定段允许设锚）
        status, body = self.api("POST", "/api/segments/%d/anchor" % seg,
                                {"measurement_id": ids[9]})
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 2)
        frames = self.by_frame(body)
        self.assertAlmostEqual(frames[8]["offset"], 4.00, places=6)   # 8×(8.5−8.0)，按 v2 重算
        self.assertAlmostEqual(frames[9]["offset"], 0.00, places=6)   # 锚点清零
        self.assertAlmostEqual(frames[10]["offset"], 0.50, places=6)  # 锚后一节 8.5−8.0

        # 缓存已换到 v2：再取复算 JSON 结果一致
        _, again = self.api("GET", "/api/segments/%d" % seg)
        self.assertEqual(again["version"], 2)
        frames = self.by_frame(again)
        self.assertAlmostEqual(frames[8]["offset"], 4.00, places=6)
        self.assertAlmostEqual(frames[9]["offset"], 0.00, places=6)
        self.assertAlmostEqual(frames[10]["offset"], 0.50, places=6)

    def test_reanchor_same_version(self):
        """同版本改锚：锚点前沿保持缓存值，后方以新锚清零续算。"""
        seg = self.create_segment("同版改锚")
        ids = self.add_sprockets(seg, 12, 8.5)
        # 默认 v1（标称节距 7.62）：帧 i 偏移 0.88i
        status, body = self.api("POST", "/api/segments/%d/anchor" % seg,
                                {"measurement_id": ids[9]})
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        frames = self.by_frame(body)
        self.assertAlmostEqual(frames[9]["offset"], 0.00, places=6)
        self.assertAlmostEqual(frames[10]["offset"], 0.88, places=6)

        # 版本未变，改锚到 frame 5
        status, body = self.api("POST", "/api/segments/%d/anchor" % seg,
                                {"measurement_id": ids[5]})
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        frames = self.by_frame(body)
        self.assertAlmostEqual(frames[4]["offset"], 3.52, places=6)   # 0.88×4，缓存前缀
        self.assertAlmostEqual(frames[5]["offset"], 0.00, places=6)   # 新锚清零
        self.assertAlmostEqual(frames[6]["offset"], 0.88, places=6)
        self.assertAlmostEqual(frames[9]["offset"], 3.52, places=6)   # 0.88×9−0.88×5
        self.assertAlmostEqual(frames[10]["offset"], 4.40, places=6)

    def test_locked_segment_delete_returns_409(self):
        """已锁区间只读：删除测量点/动作返回 409，解锁后恢复。"""
        seg = self.create_segment("锁定只读")
        ids = self.add_sprockets(seg, 4, 8.0)
        status, body = self.api("POST", "/api/segments/%d/actions" % seg,
                                {"frame_index": 1, "type": "slow",
                                 "factor": 2.0})
        self.assertEqual(status, 201)
        action_id = body["id"]
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200)
        self.assertTrue(body["locked"])

        status, body = self.api("DELETE", "/api/measurements/%d" % ids[0])
        self.assertEqual(status, 409)
        self.assertIn("只读", body["error"])
        status, body = self.api("DELETE", "/api/actions/%d" % action_id)
        self.assertEqual(status, 409)
        self.assertIn("只读", body["error"])

        # 解锁后即可删除
        status, body = self.api("POST", "/api/segments/%d/unlock" % seg, {})
        self.assertEqual(status, 200)
        status, body = self.api("DELETE", "/api/measurements/%d" % ids[0])
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"])

    def test_hold_across_brittle_edge_cannot_lock(self):
        """托带跨度跨过脆裂边：锁定校验拒绝（422），段保持未锁。"""
        seg = self.create_segment("托带跨脆裂边")
        self.add_sprockets(seg, 6, 8.0)                # 帧 0..4，x = 0,8,16,24,32
        self.add_measurement(seg, 0, 20.0, kind="brittle")  # 脆裂边在帧 2 跨度 16–24 内
        status, body = self.api("POST", "/api/segments/%d/actions" % seg,
                                {"frame_index": 2, "type": "hold", "span": 1})
        self.assertEqual(status, 201)

        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertFalse(body["locked"])
        self.assertTrue(any("脆裂边" in e for e in body["errors"]))
        _, cur = self.api("GET", "/api/segments/%d" % seg)
        self.assertEqual(cur["segment"]["locked"], 0)

        # 对照：脆裂边移出托带跨度即可锁定
        seg2 = self.create_segment("托带不跨脆裂边")
        self.add_sprockets(seg2, 6, 8.0)
        self.add_measurement(seg2, 0, 30.0, kind="brittle")  # 在跨度 16–24 之外
        self.api("POST", "/api/segments/%d/actions" % seg2,
                 {"frame_index": 2, "type": "hold", "span": 1})
        status, body = self.api("POST", "/api/segments/%d/lock" % seg2, {})
        self.assertEqual(status, 200)
        self.assertTrue(body["locked"])

    def test_json_svg_transport_card_share_param_version(self):
        """复算 JSON、偏移 SVG、走带卡共用同一参数版本。"""
        seg = self.create_segment("三视图同版")
        self.add_sprockets(seg, 6, 8.0)
        # 先在 v1 锁定留下旧版缓存，再升到 v2：三处都必须报当前版本
        status, _ = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200)
        self.set_params(nominal_pitch=8.0)             # v1(7.62) -> v2
        _, params = self.api("GET", "/api/params")
        self.assertEqual(params["version"], 2)

        _, js = self.api("GET", "/api/segments/%d" % seg)
        self.assertEqual(js["version"], params["version"])

        status, svg, ctype = self.get_raw("/api/segments/%d/offset.svg" % seg)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/svg+xml")
        self.assertIn("参数版本 v%d" % params["version"], svg)

        _, card = self.api("GET", "/api/segments/%d/transport_card" % seg)
        self.assertEqual(card["version"], params["version"])
        self.assertEqual(card["segment"], "三视图同版")
        self.assertTrue(card["locked"])

    # ---------- 双边门位稳定 ----------

    def test_gate_pairing_width_center_angle(self):
        """双侧观测配对：片宽、中心线横移、画格旋角按帧给出。"""
        seg = self.create_segment("门位配对")
        self.add_bilateral(seg, 12, shift_r=0.02, skew_r=0.04)
        _, result = self.api("GET", "/api/segments/%d" % seg)
        gate = result["gate"]
        self.assertEqual(gate["pair_count"], 12)
        m0 = next(f for f in gate["measured_frames"] if f["frame"] == 0)
        m11 = next(f for f in gate["measured_frames"] if f["frame"] == 11)
        # 片宽 = 16 − 左y − 右y：首帧 16−0.3−0.3=15.4
        self.assertAlmostEqual(m0["width"], 15.4, places=6)
        # 末帧右侧多收 0.02×11：16−0.3−0.52=15.18
        self.assertAlmostEqual(m11["width"], 15.18, places=6)
        # 中心线横移 = (右y − 左y)/2：末帧 0.22/2
        self.assertAlmostEqual(m11["center"], 0.11, places=6)
        # 旋角 = atan2(右x−左x, 齿孔跨距)：齿孔 y 左0.2/右0.42，跨距 15.38
        import math
        self.assertAlmostEqual(m11["angle"],
                               math.degrees(math.atan2(0.44, 15.38)),
                               places=4)
        # 关键帧之间生成连续补偿：零关键帧时补偿恒为零
        f6 = self.gate_frame(gate, 6)
        self.assertEqual(f6["shift"], 0.0)
        self.assertEqual(f6["angle"], 0.0)

    def test_gate_continuous_compensation_between_keyframes(self):
        """关键帧之间线性插值出连续门位补偿，默认关键帧贴住实测门位。"""
        seg = self.create_segment("门位插值")
        self.add_bilateral(seg, 12, shift_r=0.02, skew_r=0.04)
        k0 = self.add_keyframe(seg, 0)                       # 默认贴 frame 0
        k11 = self.add_keyframe(seg, 11)                     # 默认贴 frame 11
        gate = self.gate_result(seg)
        self.assertAlmostEqual(self.gate_frame(gate, 0)["shift"], 0.0, 6)
        self.assertAlmostEqual(self.gate_frame(gate, 11)["shift"], 0.11, 6)
        mid = self.gate_frame(gate, 6)
        self.assertAlmostEqual(mid["shift"], 0.06, places=6)
        # 补偿贴合实测时全部帧余量为正，允许锁定
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["locked"])

    def test_gate_ambiguous_pairing_locates_first_frame(self):
        """配对多解：定位首帧并禁止锁定。"""
        seg = self.create_segment("配对多解")
        self.add_bilateral(seg, 6)
        # frame 3 左侧再来一条可用齿孔 → 同帧同侧同 kind 多解
        self.add_edge(seg, 3, "L", "sprocket", x=23.0, y=0.25)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("帧 3" in e and "配对多解" in e
                            for e in body["errors"]))
        # 把原观测标为不可用缺口后，唯一可用观测不再多解
        _, result = self.api("GET", "/api/segments/%d" % seg)
        dup = [e for e in result["edge_observations"]
               if e["frame_index"] == 3 and e["side"] == "L"
               and e["kind"] == "sprocket"][0]
        # 直接加一条不可用记录不影响；这里验证 usable=0 不参与多解判定
        self.add_edge(seg, 3, "R", "sprocket", x=22.8, y=0.21, usable=False)
        gate = self.gate_result(seg)
        self.assertIn(3, gate["ambiguous_frames"])  # 左侧仍两条可用

    def test_gate_single_side_gap_too_long(self):
        """单侧缺测连续超过上限：定位起始帧并禁止锁定。"""
        seg = self.create_segment("单侧缺测")
        self.add_bilateral(seg, 12)
        # 删掉 frame 3..7 的右侧两条观测（齿孔+片边），单侧缺 5 帧 > 4
        _, result = self.api("GET", "/api/segments/%d" % seg)
        for e in result["edge_observations"]:
            if e["side"] == "R" and 3 <= e["frame_index"] <= 7:
                self.api("DELETE", "/api/edges/%d" % e["id"])
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("帧 3" in e and "单侧" in e and "5 帧" in e
                            for e in body["errors"]))
        # 对照：缺 4 帧（== 上限）不报此项
        seg2 = self.create_segment("单侧缺测达标")
        self.add_bilateral(seg2, 12)
        _, r2 = self.api("GET", "/api/segments/%d" % seg2)
        for e in r2["edge_observations"]:
            if e["side"] == "R" and 4 <= e["frame_index"] <= 7:
                self.api("DELETE", "/api/edges/%d" % e["id"])
        gate2 = self.gate_result(seg2)
        self.assertFalse(any("单侧" in e for e in
                             self.api("GET", "/api/segments/%d" % seg2)[1]
                             ["gate_errors"]))

    def test_gate_width_jump_locates_first_frame(self):
        """片宽突变超阈值：定位突变首帧并禁止锁定。"""
        seg = self.create_segment("片宽突变")
        self.add_bilateral(seg, 12)
        # 把 frame 6 起右侧片边观测就地内收 1.0mm（不能加第二条，否则是多解）
        _, result = self.api("GET", "/api/segments/%d" % seg)
        for e in result["edge_observations"]:
            if (e["side"] == "R" and e["kind"] == "edge"
                    and e["frame_index"] >= 6):
                status, _ = self.api(
                    "POST", "/api/segments/%d/edges/%d" % (seg, e["id"]),
                    {"y": e["y"] + 1.0})
                self.assertEqual(status, 200)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("帧 6" in e and "片宽突变" in e
                            for e in body["errors"]))

    def test_gate_compensation_jump(self):
        """补偿跳变：相邻关键帧数值差超阈值，定位首帧并禁止锁定。"""
        seg = self.create_segment("补偿跳变")
        self.add_bilateral(seg, 8)
        self.add_keyframe(seg, 3, shift=0.0, angle=0.0)
        self.add_keyframe(seg, 4, shift=2.0, angle=0.0)   # Δshift 2 > 0.5
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("帧 3→4" in e and "补偿跳变" in e
                            for e in body["errors"]))

    def test_gate_crop_invasion_blocks_confirm(self):
        """裁切侵入画面：无补偿或欠补偿时余量为负，禁止锁定。"""
        seg = self.create_segment("裁切侵入")
        self.add_bilateral(seg, 12, shift_r=0.02, skew_r=0.04)
        # 不设关键帧：补偿恒零，残差旋角/横移吃光余量
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("扫描窗侵入画面" in e for e in body["errors"]))
        # 贴实测的关键帧补偿后放行（与前例一致）
        seg2 = self.create_segment("补偿到位")
        self.add_bilateral(seg2, 12, shift_r=0.02, skew_r=0.04)
        self.add_keyframe(seg2, 0)
        g = self.gate_result(seg2)
        f11 = self.gate_frame(g, 11)
        self.add_keyframe(seg2, 11, shift=f11["meas_center"],
                          angle=f11["meas_angle"])
        status, body = self.api("POST", "/api/segments/%d/lock" % seg2, {})
        self.assertEqual(status, 200, body)

    def test_gate_keyframe_tweak_only_recomputes_neighbor_zones(self):
        """调整某个关键帧：只相邻区间重算；参数换版则全部重算。"""
        seg = self.create_segment("局部重算")
        self.add_bilateral(seg, 12, shift_r=0.02, skew_r=0.04)
        k0 = self.add_keyframe(seg, 0, shift=0.0, angle=0.0)
        k11 = self.add_keyframe(seg, 11, shift=0.11, angle=1.6)
        # 插入 frame 5：只有 0-5 与 5-11 两个区间重算
        status, body = self.api(
            "POST", "/api/segments/%d/keyframes" % seg,
            {"frame_index": 5, "shift": 0.05, "angle": 0.8})
        self.assertEqual(status, 201)
        self.assertEqual(sorted(body["gate"]["recomputed_zones"]),
                         ["zone:0-5", "zone:5-11"])
        k5 = next(k["id"] for k in body["gate"]["keyframes"]
                  if k["frame_index"] == 5)
        # 再取一次：版本/观测/端点都没变，全部区间命中缓存
        gate = self.gate_result(seg)
        self.assertEqual(gate["recomputed_zones"], [])
        # 拖动 frame 5：仍只相邻两区间
        status, body = self.patch_keyframe(seg, k5, shift=0.07)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["recomputed_zones"]),
                         ["zone:0-5", "zone:5-11"])
        # 紧接着再查：零重算
        self.assertEqual(self.gate_result(seg)["recomputed_zones"], [])
        # 参数换版：全部区间失效重算
        self.set_params(safe_margin=0.4)
        gate = self.gate_result(seg)
        self.assertEqual(sorted(gate["recomputed_zones"]),
                         ["zone:0-5", "zone:5-11"])

    def test_gate_locked_region_readonly(self):
        """锁定区：双边观测与关键帧写入全部 409，解锁后恢复。"""
        seg = self.create_segment("门位锁定只读")
        self.add_bilateral(seg, 6)
        k0 = self.add_keyframe(seg, 0)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)

        status, _ = self.api(
            "POST", "/api/segments/%d/edges" % seg,
            {"frame_index": 1, "side": "L", "kind": "sprocket",
             "x": 7.6, "y": 0.2})
        self.assertEqual(status, 409)
        status, _ = self.api(
            "POST", "/api/segments/%d/keyframes" % seg, {"frame_index": 3})
        self.assertEqual(status, 409)
        status, _ = self.patch_keyframe(seg, k0, shift=0.5)
        self.assertEqual(status, 409)
        _, result = self.api("GET", "/api/segments/%d" % seg)
        eid = result["edge_observations"][0]["id"]
        self.assertEqual(self.api("DELETE", "/api/edges/%d" % eid)[0], 409)
        self.assertEqual(self.api("DELETE", "/api/keyframes/%d" % k0)[0], 409)

        self.api("POST", "/api/segments/%d/unlock" % seg, {})
        status, _ = self.patch_keyframe(seg, k0, shift=0.5)
        self.assertEqual(status, 200)

    def test_gate_three_views_share_version_and_observations(self):
        """复算 JSON、稳定轨迹 SVG、走带卡引用同一参数版本与双边观测。"""
        seg = self.create_segment("门位三图同源")
        self.add_bilateral(seg, 8, shift_r=0.02, skew_r=0.03)
        self.add_keyframe(seg, 0)
        self.add_keyframe(seg, 7, shift=0.07, angle=0.8)
        # 先在 v1 锁定缓存，再升 v2
        status, _ = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200)
        self.set_params(safe_margin=0.4)
        _, params = self.api("GET", "/api/params")

        _, js = self.api("GET", "/api/segments/%d" % seg)
        sig = js["gate"]["obs_sig"]
        self.assertEqual(js["version"], params["version"])
        self.assertTrue(sig)

        status, svg, ctype = self.get_raw(
            "/api/segments/%d/gate.svg" % seg)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/svg+xml")
        self.assertIn("参数版本 v%d" % params["version"], svg)
        self.assertIn(sig, svg)

        _, card = self.api("GET",
                           "/api/segments/%d/transport_card" % seg)
        self.assertEqual(card["version"], params["version"])
        self.assertEqual(card["obs_sig"], sig)
        self.assertEqual(card["gate"]["pair_count"], 8)

    def test_gate_duplicate_keyframe_rejected(self):
        """同一帧重复插入关键帧返回 400；改值走 PATCH。"""
        seg = self.create_segment("关键帧唯一")
        self.add_bilateral(seg, 6)
        kf = self.add_keyframe(seg, 2, shift=0.01, angle=0.0)
        status, body = self.api(
            "POST", "/api/segments/%d/keyframes" % seg,
            {"frame_index": 2, "shift": 0.2})
        self.assertEqual(status, 400)
        self.assertIn("已有关键帧", body["error"])
        status, body = self.patch_keyframe(seg, kf, shift=0.2, angle=0.3)
        self.assertEqual(status, 200)
        k = next(k for k in body["keyframes"] if k["frame_index"] == 2)
        self.assertAlmostEqual(k["shift"], 0.2, 6)
        self.assertAlmostEqual(k["angle"], 0.3, 6)

    def test_gate_scan_window_corner_geometry(self):
        """扫描窗预览角点：纯函数复现像素位置，默认数据须落在画布内。

        前端 drawGate 曾把画格半尺寸先折像素、gMap 再折一次（重复缩放），
        默认 16mm 数据四角全部出界。此处直接跑 Node 几何回归脚本，
        环境无 node 时跳过。
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过前端几何回归")
        proc = subprocess.run(
            [node, os.path.join(BASE_DIR, "static", "test_gate_geo.js")],
            capture_output=True, text=True, cwd=BASE_DIR)
        self.assertEqual(proc.returncode, 0,
                         "门位角点几何回归失败：\n" + proc.stdout
                         + proc.stderr)

    # ---------- 焦面排程 ----------

    def _focus_segment(self, name, n=12, pitch=7.62, **kw):
        seg = self.create_segment(name)
        self.add_sprockets(seg, n + 1, pitch)
        return seg

    def test_focus_envelope_recommended_coverage(self):
        """翘曲包络按帧拟合：推荐策略咬中点，景深窗算清晰覆盖比例。"""
        seg = self._focus_segment("焦面包络")
        # 帧 0 包络厚 0.1（景深 0.25 全覆盖），帧 11 包络厚 0.6（只罩中央）
        for fi in range(12):
            w = 0.05 + 0.25 * fi / 11
            self.add_height(seg, fi, "C", 50.0)
            for pos, s in (("TL", -1), ("TR", 1), ("BL", 1), ("BR", -1)):
                self.add_height(seg, fi, pos, 50.0 + s * w)
        focus = self.focus_result(seg)
        r0 = self.focus_row(focus, 0)
        r11 = self.focus_row(focus, 11)
        self.assertAlmostEqual(r0["z_near"], 49.95, places=6)
        self.assertAlmostEqual(r0["z_far"], 50.05, places=6)
        self.assertAlmostEqual(r0["target"], 50.0, places=6)
        self.assertEqual(r0["coverage"], 1.0)
        # 末帧 ±0.3 角点距焦位 0.3 > 景深半窗 0.125，只有中央在窗内
        self.assertAlmostEqual(r11["coverage"], 0.2, places=6)
        self.assertTrue(all(r["settled"] for r in focus["frames"]))

    def test_focus_three_strategies(self):
        """恒定 / 推荐 / 人工三策略：恒焦全段一个焦位，人工锚间线性插值。"""
        seg = self._focus_segment("焦面三策略")
        self.cover_focus_frames(seg, 12, zc=50.0, warp=0.05)

        # 恒定：首锚 49.9 全段不变
        self.set_focus_strategy(seg, "constant")
        self.add_focus_anchor(seg, 2, focus=49.9)
        focus = self.focus_result(seg)
        self.assertTrue(all(abs(r["focus"] - 49.9) < 1e-9
                            for r in focus["frames"]))

        # 清锚换人工：0→11 由 50.0 线性到 50.2
        _, result = self.api("GET", "/api/segments/%d" % seg)
        for a in result["focus_anchors"]:
            self.assertEqual(
                self.api("DELETE", "/api/focus_anchors/%d" % a["id"])[0], 200)
        self.set_focus_strategy(seg, "manual")
        self.add_focus_anchor(seg, 0, focus=50.0)
        self.add_focus_anchor(seg, 11, focus=50.2)
        focus = self.focus_result(seg)
        self.assertAlmostEqual(self.focus_row(focus, 5)["focus"],
                               50.0 + 0.2 * 5 / 11, places=6)
        self.assertAlmostEqual(self.focus_row(focus, 0)["focus"], 50.0, 6)
        self.assertAlmostEqual(self.focus_row(focus, 11)["focus"], 50.2, 6)
        # 首锚默认值咬住该帧包络中点
        seg2 = self._focus_segment("默认锚")
        self.cover_focus_frames(seg2, 8, zc=51.0, warp=0.1)
        aid = self.add_focus_anchor(seg2, 3)   # 不给 focus
        _, res = self.api("GET", "/api/segments/%d" % seg2)
        a = next(x for x in res["focus_anchors"] if x["id"] == aid)
        self.assertAlmostEqual(a["focus"], 51.0, places=6)

    def test_focus_anchor_tweak_only_recomputes_neighbor_zones(self):
        """编辑焦点锚点：只更新夹在相邻锚点间的区间；再查零重算。"""
        seg = self._focus_segment("焦面局部重算")
        self.cover_focus_frames(seg, 12, warp=0.02)
        self.set_focus_strategy(seg, "manual")
        self.add_focus_anchor(seg, 0, focus=50.0)
        self.add_focus_anchor(seg, 5, focus=50.1)
        self.add_focus_anchor(seg, 11, focus=50.2)
        # 插锚后相邻两区间重算
        status, body = self.api(
            "POST", "/api/segments/%d/focus_anchors" % seg,
            {"frame_index": 8, "focus": 50.15})
        self.assertEqual(status, 201)
        self.assertEqual(sorted(body["focus"]["recomputed_zones"]),
                         ["manual:zone:5-8", "manual:zone:8-11"])
        # 再查零重算
        self.assertEqual(self.focus_result(seg)["recomputed_zones"], [])
        # 拖动 frame 5：只 0-5 与 5-8
        _, res = self.api("GET", "/api/segments/%d" % seg)
        k5 = next(a["id"] for a in res["focus_anchors"]
                  if a["frame_index"] == 5)
        status, body = self.api(
            "POST", "/api/segments/%d/focus_anchors/%d" % (seg, k5),
            {"focus": 50.08})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["recomputed_zones"]),
                         ["manual:zone:0-5", "manual:zone:5-8"])
        self.assertEqual(self.focus_result(seg)["recomputed_zones"], [])

    def test_focus_strategy_switch_uses_same_envelope(self):
        """切换策略不串味：缓存只存包络，目标焦位按新策略首帧即正确。"""
        seg = self._focus_segment("焦面切策略")
        self.cover_focus_frames(seg, 12, warp=0.02)
        self.set_focus_strategy(seg, "constant")
        self.add_focus_anchor(seg, 3, focus=49.5)
        focus = self.focus_result(seg)
        self.assertTrue(all(r["focus"] == 49.5 for r in focus["frames"]))
        # 切回推荐：目标应回到各帧包络中点 50.0，而不是沿用 49.5
        self.set_focus_strategy(seg, "recommended")
        focus = self.focus_result(seg)
        self.assertTrue(all(abs(r["target"] - 50.0) < 1e-9
                            for r in focus["frames"]))
        # h_sig 保持不变
        self.assertTrue(focus["h_sig"])

    def test_focus_ambiguous_height_locates_first_frame(self):
        """测高点归帧歧义：同帧同点位多条可用，定位首帧并拒绝锁定。"""
        seg = self._focus_segment("焦面归帧歧义")
        self.cover_focus_frames(seg, 8)
        self.add_height(seg, 3, "C", 50.11)   # frame 3 中央第二条
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("帧 3" in e and "归帧有歧义" in e
                            for e in body["errors"]))
        # 标缺一条后多解消除，可锁定
        _, res = self.api("GET", "/api/segments/%d" % seg)
        dup = [h for h in res["height_observations"]
               if h["frame_index"] == 3 and h["pos"] == "C"]
        status, _ = self.api(
            "POST", "/api/segments/%d/heights/%d" % (seg, dup[-1]["id"]),
            {"usable": False})
        self.assertEqual(status, 200)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)

    def test_focus_splice_baseline_break(self):
        """接片基准断裂：缺 SB/SA 或前后物距跳变超景深，定位接片帧。"""
        # 条带圈记了接片但无 SB/SA
        seg = self._focus_segment("焦面接片缺基准")
        self.cover_focus_frames(seg, 12)
        self.add_measurement(seg, 0, 5 * 7.62, kind="splice")
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("帧 5" in e and "接片基准断裂" in e
                            for e in body["errors"]))
        # 补上 SB/SA 但跳变 1.0mm > 景深 0.25
        self.add_height(seg, 5, "SB", 50.0)
        self.add_height(seg, 6, "SA", 51.0)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("物距跳变" in e for e in body["errors"]))
        # 加大景深到 1.2 后跳变被景深吞掉，放行
        self.set_params(lens_dof=1.2)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)

    def test_focus_data_gap_too_long(self):
        """数据空档：连续无直接测高超过 6 帧，定位空档首帧。"""
        seg = self._focus_segment("焦面空档")
        for fi in (0, 1, 10, 11):
            self.add_height(seg, fi, "C", 50.0)
            for pos in ("TL", "TR", "BL", "BR"):
                self.add_height(seg, fi, pos, 50.0)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("数据空档" in e and "帧 2" in e
                            for e in body["errors"]))
        # 对照：空档 6 帧（==上限）不报
        seg2 = self._focus_segment("焦面空档达标")
        for fi in list(range(3)) + list(range(9, 12)):
            self.add_height(seg2, fi, "C", 50.0)
            for pos in ("TL", "TR", "BL", "BR"):
                self.add_height(seg2, fi, pos, 50.0)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg2, {})
        self.assertEqual(status, 200, body)

    def test_focus_coverage_below_threshold_blocks_lock(self):
        """焦域覆盖不足：清晰覆盖低于 80% 定位首帧；加大景深后放行。"""
        seg = self._focus_segment("焦域覆盖")
        for fi in range(12):
            w = 0.05 + 0.25 * fi / 11
            self.add_height(seg, fi, "C", 50.0)
            for pos, s in (("TL", -1), ("TR", 1), ("BL", 1), ("BR", -1)):
                self.add_height(seg, fi, pos, 50.0 + s * w)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("焦域覆盖不足" in e for e in body["errors"]))
        self.set_params(lens_dof=0.7)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)

    def test_focus_motor_cannot_settle(self):
        """机构来不及稳定：相邻帧焦位需求超出速度×(dt−静定)；降速解救。"""
        seg = self._focus_segment("焦面来不及")
        for fi in range(12):
            zc = 50.0 if fi < 6 else 55.0
            for pos in ("C", "TL", "TR", "BL", "BR"):
                self.add_height(seg, fi, pos, zc)
        # 默认 8mm/s、静定 0.02s：帧间隔 0.0417s 最多走 0.17mm，5mm 必失败
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("来不及稳定" in e for e in body["errors"]))
        # 帧 5 降速 20 倍：dt≈0.833s，电机可走 8×0.813≈6.5mm > 5mm
        status, _ = self.api("POST", "/api/segments/%d/actions" % seg,
                             {"frame_index": 5, "type": "slow", "factor": 20.0})
        self.assertEqual(status, 201)
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)

    def test_focus_position_out_of_range(self):
        """所需焦位超出调焦范围：首帧拦截。"""
        seg = self._focus_segment("焦面超程")
        self.cover_focus_frames(seg, 8, zc=80.0)   # 默认范围 40–60
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("超出调焦范围" in e for e in body["errors"]))

    def test_focus_manual_without_anchor_rejected(self):
        """人工策略无锚点拒绝锁定；推荐/恒定不受此限。"""
        seg = self._focus_segment("人工无锚")
        self.cover_focus_frames(seg, 8)
        self.set_focus_strategy(seg, "manual")
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 422)
        self.assertTrue(any("人工策略" in e and "焦点锚点" in e
                            for e in body["errors"]))
        self.set_focus_strategy(seg, "recommended")
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)

    def test_focus_three_views_share_height_draft_and_strategy(self):
        """焦域热图、重演 JSON、走带卡固定同一测高稿指纹与策略版本。"""
        seg = self._focus_segment("焦面三图同源")
        self.cover_focus_frames(seg, 8, zc=50.0, warp=0.05)
        self.set_focus_strategy(seg, "manual")
        self.add_focus_anchor(seg, 0, focus=50.0)
        self.add_focus_anchor(seg, 7, focus=50.05)
        # v1 锁定缓存，再升 v2
        self.assertEqual(
            self.api("POST", "/api/segments/%d/lock" % seg, {})[0], 200)
        self.set_params(lens_dof=0.4)

        _, js = self.api("GET", "/api/segments/%d" % seg)
        hsig = js["focus"]["h_sig"]
        self.assertEqual(js["version"], 2)
        self.assertEqual(js["focus"]["strategy"], "manual")

        status, svg, ctype = self.get_raw(
            "/api/segments/%d/focus.svg" % seg)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/svg+xml")
        self.assertIn("参数版本 v2", svg)
        self.assertIn(hsig, svg)

        _, replay = self.api(
            "GET", "/api/segments/%d/focus_replay" % seg)
        self.assertEqual(replay["h_sig"], hsig)
        self.assertEqual(replay["version"], 2)
        self.assertEqual(replay["strategy"], "manual")
        self.assertEqual(len(replay["frames"]),
                         len(js["focus"]["frames"]))
        self.assertEqual(replay["lens"]["dof"], 0.4)

        _, card = self.api("GET",
                           "/api/segments/%d/transport_card" % seg)
        self.assertEqual(card["h_sig"], hsig)
        self.assertEqual(card["focus_strategy"], "manual")
        self.assertEqual(card["version"], 2)
        self.assertEqual(card["focus"]["anchors"], 2)

    def test_focus_height_change_invalidates_cache(self):
        """测高稿增删改：h_sig 变化，区间缓存失效重算。"""
        seg = self._focus_segment("焦面测高改版")
        self.cover_focus_frames(seg, 8, warp=0.02)
        focus = self.focus_result(seg)
        sig0 = focus["h_sig"]
        self.assertEqual(focus["recomputed_zones"], [])
        # 多一条中央测高 → 均值变化、h_sig 变；POST 响应带回重算区间
        status, body = self.api(
            "POST", "/api/segments/%d/heights" % seg,
            {"frame_index": 4, "pos": "C", "z": 50.02})
        self.assertEqual(status, 201)
        self.assertNotEqual(body["focus"]["h_sig"], sig0)
        self.assertTrue(body["focus"]["recomputed_zones"])
        # 再取已命中新缓存（零重算），指纹保持
        focus = self.focus_result(seg)
        self.assertNotEqual(focus["h_sig"], sig0)
        self.assertEqual(focus["recomputed_zones"], [])

    def test_focus_locked_region_readonly(self):
        """锁定区：测高点与焦点锚点写入/删除全部 409。"""
        seg = self._focus_segment("焦面锁定只读")
        self.cover_focus_frames(seg, 6)
        aid = self.add_focus_anchor(seg, 0, focus=50.0)
        self.assertEqual(
            self.api("POST", "/api/segments/%d/lock" % seg, {})[0], 200)
        status, _ = self.api(
            "POST", "/api/segments/%d/heights" % seg,
            {"frame_index": 1, "pos": "C", "z": 50.0})
        self.assertEqual(status, 409)
        status, _ = self.api(
            "POST", "/api/segments/%d/focus_anchors" % seg,
            {"frame_index": 2, "focus": 50.0})
        self.assertEqual(status, 409)
        status, _ = self.api(
            "POST", "/api/segments/%d/focus_anchors/%d" % (seg, aid),
            {"focus": 51.0})
        self.assertEqual(status, 409)
        _, res = self.api("GET", "/api/segments/%d" % seg)
        hid = res["height_observations"][0]["id"]
        self.assertEqual(self.api("DELETE", "/api/heights/%d" % hid)[0], 409)
        self.assertEqual(
            self.api("DELETE", "/api/focus_anchors/%d" % aid)[0], 409)
        # 策略切换也不允许（会改变锁定结果）
        status, _ = self.api(
            "POST", "/api/segments/%d/focus_strategy" % seg,
            {"strategy": "constant"})
        self.assertEqual(status, 409)

    def test_focus_empty_segment_not_constrained(self):
        """无测高稿：焦面校验整项跳过，旧段锁定行为不变。"""
        seg = self._focus_segment("无测高旧段")
        status, body = self.api("POST", "/api/segments/%d/lock" % seg, {})
        self.assertEqual(status, 200, body)
        _, res = self.api("GET", "/api/segments/%d" % seg)
        self.assertEqual(res["focus_errors"], [])
        import compute as _compute
        self.assertEqual(res["focus"]["h_sig"], _compute.height_signature([]))


if __name__ == "__main__":
    unittest.main()
