# 走带编排（胶片修复）

老式电影胶片收缩后的走带编排工具。Python 标准库承接请求，sqlite3 存测量稿，
浏览器脚本在 Canvas 中同步条带、齿孔和画格。

## 运行

    python3 server.py [端口]     # 默认 8000，打开 http://127.0.0.1:8000

## 测试

    python3 -m unittest test_regression -v

标准库回归测试（临时 SQLite + 本地 HTTP 服务，不触碰 measurements.db）：
参数换版后首次设锚（v1 缓存 → v2 重算，frame 8/9/10 偏移 4.00/0.00/0.50）、
同版本改锚、锁定段删除返回 409、托带跨脆裂边拒绝锁定、
复算 JSON / 偏移 SVG / 走带卡共用同一参数版本。

## 使用流程

1. 顶部录入片宽、标称节距、镜头窗口（偏移/尺寸）与牵引上限，保存后参数版本号 +1。
2. 建段后选点取工具，在条带图上逐段点取齿孔中心；接片、缺口、翘曲、脆化边
   以圈记标出。右键删除最近测量点。
3. 时间尺上插入动作：降速（倍率）、空过一齿、停机托带（跨帧数、恢复时码）。
4. 「复算」沿实测节距累加收缩，逐帧求画格偏移、啮合深度和校正时码，
   首个失步位置以红线钉回条带图。
5. 「锁定该段」前自动校验，命中任一规则即拒绝：
   编号回退 / 接片两侧无法唯一对齐 / 缺测跨度超过 8 个节距 /
   托带动作跨过脆裂边 / 校正时码重叠。
6. 已锁区间只读（写入返回 409）；改锚点只续算后方帧，锚点前沿保持锁定时的缓存值。

## 接口

- `GET/POST /api/params` — 全局参数（POST 使版本号 +1）
- `GET/POST /api/segments`，`GET /api/segments/<id>` — 复算 JSON
- `POST /api/segments/<id>/measurements|actions|lock|unlock|anchor`
- `GET /api/segments/<id>/offset.svg` — 偏移曲线
- `GET /api/segments/<id>/transport_card` — 走带卡
- `DELETE /api/measurements/<id>`、`DELETE /api/actions/<id>`

走带卡、偏移 SVG、复算 JSON 共享 `compute.py` 同一版参数与计算结果。
