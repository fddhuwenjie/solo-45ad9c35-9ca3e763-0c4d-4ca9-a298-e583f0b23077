# 走带编排（胶片修复）

老式电影胶片收缩后的走带编排工具。Python 标准库承接请求，sqlite3 存测量稿，
浏览器脚本在 Canvas 中同步条带、齿孔和画格。

## 运行

    python3 server.py [端口]     # 默认 8000，打开 http://127.0.0.1:8000

## 测试

    python3 -m unittest test_regression -v
    node static/test_gate_geo.js        # 扫描窗画格角点几何（无服务/浏览器依赖）

标准库回归测试（临时 SQLite + 本地 HTTP 服务，不触碰 measurements.db）：
参数换版后首次设锚（v1 缓存 → v2 重算，frame 8/9/10 偏移 4.00/0.00/0.50）、
同版本改锚、锁定段删除返回 409、托带跨脆裂边拒绝锁定、
双边门位配对/插值/局部重算/五类校验拦截、扫描窗角点像素位置
（node 存在时由 Python 套件一并跑 `static/test_gate_geo.js`）、
焦面排程翘曲包络/三策略/锚点局部重算/五类拦截/三视图同源（共 32 项），
复算 JSON / 偏移 SVG / 稳定轨迹 SVG / 走带卡共用同一参数版本与双边观测。

## 使用流程

1. 顶部录入片宽、标称节距、镜头窗口（偏移/尺寸）、牵引上限与安全余量，
   保存后参数版本号 +1。
2. 建段后选点取工具，在条带图上逐段点取齿孔中心；接片、缺口、翘曲、脆化边
   以圈记标出。右键删除最近测量点。
3. 时间尺上插入动作：降速（倍率）、空过一齿、停机托带（跨帧数、恢复时码）。
4. 「复算」沿实测节距累加收缩，逐帧求画格偏移、啮合深度和校正时码，
   首个失步位置以红线钉回条带图。
5. 「双边门位稳定工作区」逐帧标记左右齿孔中心、片边与不可用缺口；
   系统按帧配对两侧观测，计算片宽、中心线横移与画格旋角。在画格上拖放
   稳定关键帧（上下=横移，顶部把手=旋角），关键帧之间线性插值出连续
   门位补偿，画布即时预览扫描窗与四边安全裁切余量。
6. 「锁定该段」前自动校验，命中任一规则即拒绝（门位问题均定位首帧）：
   编号回退 / 接片两侧无法唯一对齐 / 缺测跨度超过 8 个节距 /
   托带动作跨过脆裂边 / 校正时码重叠 /
   门位配对多解（同帧同侧多条可用）/ 单侧缺测连续超过 4 帧 /
   片宽突变（>0.50mm）/ 补偿跳变（横移 >0.50mm 或旋角 >1°）/
   扫描窗裁切侵入画面（最小余量为负）。
7. 已锁区间只读（写入返回 409）；改锚点只续算后方帧，锚点前沿保持锁定时
   的缓存值。调整某个门位关键帧只重算相邻区间，复算 JSON、稳定轨迹 SVG
   与走带卡引用同一参数版本与双边观测指纹。

## 焦面排程

胶片受潮横向拱起后，同一画格中央与四角落在不同物距上；门位补偿稳得住
构图、稳不住焦面。焦面排程在走带编排台平行提供第二套补偿：

1. 顶部追加镜头参数：镜头景深、调焦近端/远端（调焦范围）、电机速度
   （mm/s）、静定时长（s）。保存同样使参数版本号 +1。
2. 「焦面排程工作区」逐帧在画格五点位（中央 C、四角 TL/TR/BL/BR）
   录入测高（物距 mm）；接处在接缝两侧录「接片前 SB / 接片后 SA」
   基准点。右键删除最近测高。
3. 后台对每个点位沿帧线性插值，拟合逐帧翘曲包络 [z_near, z_far]，
   按帧给出清晰覆盖比例（景深窗 [focus±dof/2] 罩住的测点比）、
   所需焦位、下达焦位与电机速度（沿校正时间尺，降速/托带天然给电机
   更多移动时间）。
4. 三种策略对照切换：**恒定**（全段一个焦位，取首锚焦位或包络总中值）、
   **推荐**（逐帧咬包络中点，受电机速度/静定约束）、**人工**（时间轴
   焦点锚点之间线性插值，锚点可上下拖动）。编辑锚点仅更新夹在相邻
   锚点间的区间结果（区间缓存，参数版本 + 测高稿指纹 h_sig + 锚点端点
   签名三者一致才复用）。
5. 锁定前焦面校验命中任一规则即拒绝，并停在首个受影响画格（热图红线
   钉回）：测高点归帧歧义（同帧同位多条可用）/ 接片基准断裂（缺 SB、
   SA 或前后物距跳变超一个景深）/ 数据空档过长（连续无直接测高 > 6 帧）
   / 焦域覆盖不足（清晰覆盖 < 80%）/ 机构来不及稳定（速度超限、静定
   不足或焦位超出调焦范围）。
6. 焦域热图（`focus.svg`）、重演 JSON（`focus_replay`）与走带卡固定
   同一测高稿指纹 h_sig、同一策略与参数版本。无测高稿的旧段焦面校验
   整项跳过，不产生新约束。

## 接口

- `GET/POST /api/params` — 全局参数（含安全余量 safe_margin、镜头景深
  lens_dof、调焦范围 focus_near/focus_far、电机速度 motor_speed、
  静定时长 settle_time，POST 使版本号 +1；缺省字段保留现值）
- `GET/POST /api/segments`，`GET /api/segments/<id>` — 复算 JSON（含 gate 块）
- `POST /api/segments/<id>/measurements|actions|lock|unlock|anchor`
- `GET /api/segments/<id>/offset.svg` — 偏移曲线
- `GET /api/segments/<id>/gate.svg` — 双边门位稳定轨迹
- `GET /api/segments/<id>/transport_card` — 走带卡（含门位配对数与观测指纹）
- `POST /api/segments/<id>/edges` — 双边观测（frame_index/side/kind/x/y/usable）
- `POST /api/segments/<id>/edges/<eid>` — 就地修改（标缺/改坐标）
- `POST /api/segments/<id>/keyframes`、`POST /api/segments/<id>/keyframes/<id>`
  — 稳定关键帧（默认贴实测门位；拖放改 shift/angle，只重算相邻区间）
- `POST /api/segments/<id>/heights`、`POST /api/segments/<id>/heights/<id>`
  — 画格测高（frame_index/pos=C|TL|TR|BL|BR|SB|SA/z/usable）
- `DELETE /api/heights/<id>`
- `POST /api/segments/<id>/focus_anchors`、`POST /api/segments/<id>/focus_anchors/<id>`
  — 焦点锚点（默认咬包络中点；拖放改 focus，只重算夹在相邻锚点间的区间）
- `DELETE /api/focus_anchors/<id>`
- `POST /api/segments/<id>/focus_strategy` — 恒定 constant / 推荐 recommended / 人工 manual
- `GET /api/segments/<id>/focus.svg` — 焦域热图（包络、景深窗、覆盖比例）
- `GET /api/segments/<id>/focus_replay` — 焦面重演 JSON（逐帧焦位/速度/覆盖）
- `DELETE /api/measurements/<id>`、`DELETE /api/actions/<id>`
- `DELETE /api/edges/<id>`、`DELETE /api/keyframes/<id>`

走带卡、偏移 SVG、稳定轨迹 SVG、复算 JSON 共享 `compute.py` 同一版参数、
同一双边观测指纹（obs_sig）与门位区间缓存（参数版本/观测/关键帧端点签名
三者一致才复用）；焦域热图、重演 JSON、走带卡另固定同一测高稿指纹
（h_sig）、同一策略与参数版本，焦面区间缓存按版本/测高稿/锚点端点签名
复用（缓存体只存与策略无关的翘曲包络，切换策略首帧即正确）。
