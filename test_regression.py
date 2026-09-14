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
                "window_size", "traction_limit")


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


if __name__ == "__main__":
    unittest.main()
