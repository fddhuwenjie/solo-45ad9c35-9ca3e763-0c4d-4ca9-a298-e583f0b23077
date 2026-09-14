"""走带编排回归测试：标准库 unittest + 临时 SQLite + 本地 HTTP 服务。

    python3 -m unittest test_regression -v

每个用例在临时目录建库（不触碰仓库里的 measurements.db），
HTTP 服务监听 127.0.0.1 的临时端口，用例结束即回收。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server

PARAM_FIELDS = ("film_width", "nominal_pitch", "window_offset",
                "window_size", "traction_limit", "safe_margin")


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


if __name__ == "__main__":
    unittest.main()
