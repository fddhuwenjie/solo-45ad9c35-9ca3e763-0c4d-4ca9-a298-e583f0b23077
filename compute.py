"""走带编排计算核心。

JSON API、偏移 SVG、走带卡共用这里的函数，保证同一版参数得到同一份结果。
所有长度单位与录入一致（毫米），时间在内部以秒计。
"""

FPS = 24.0                 # 基准片门速率
BASE_MESH_MM = 0.30        # 标称节距下的啮合深度
MESH_TOLERANCE_MM = 0.25   # 节距误差全部吃掉啮合深度的容差
MAX_GAP_PITCHES = 8        # 缺测跨度上限（以标称节距计）

ACTION_SLOW = "slow"   # 降速：factor 倍时长
ACTION_SKIP = "skip"   # 空过一齿：走两齿只记一帧
ACTION_HOLD = "hold"   # 停机托带：走带停、时码继续


def sorted_sprockets(measurements):
    pts = [m for m in measurements if m["kind"] == "sprocket"]
    return sorted(pts, key=lambda m: m["x"])


def markers(measurements, kind):
    return sorted((m for m in measurements if m["kind"] == kind),
                  key=lambda m: m["x"])


def build_frames(params, measurements, actions, anchor_id=None, prefix=None):
    """沿实测节距累加收缩，逐帧求偏移、啮合深度、校正时码。

    anchor_id 给定后，anchor 之前的帧取自 prefix（上次计算缓存），
    只续算后方帧；anchor 处偏移清零作为新基准。
    """
    nominal = params["nominal_pitch"]
    traction = params["traction_limit"]
    sprockets = sorted_sprockets(measurements)

    # 每齿间实测节距；缺测跨度（两端点间无齿孔）在校验阶段处理，
    # 这里凡相邻齿孔都产生一帧。
    frames = []
    for i in range(len(sprockets) - 1):
        a, b = sprockets[i], sprockets[i + 1]
        pitch = b["x"] - a["x"]
        frames.append({
            "frame": i,
            "x": a["x"],                 # 画格首缘在条带上的位置
            "pitch": pitch,
            "shrink": pitch / nominal,   # 收缩率（<1 为收缩）
            "sprocket_id": a["id"],
        })

    # 逐帧累加收缩：实际位置相对标称节拍的漂移
    drift = 0.0
    for f in frames:
        f["offset"] = drift
        drift += f["pitch"] - nominal

    # 啮合深度：节距误差按容差折算
    for f in frames:
        err = abs(f["pitch"] - nominal)
        f["mesh"] = max(0.0, BASE_MESH_MM * (1.0 - err / MESH_TOLERANCE_MM))

    # 时间尺动作：frame -> 动作列表
    act_by_frame = {}
    for a in actions:
        act_by_frame.setdefault(a["frame_index"], []).append(a)

    # 校正时码：降速拉长、空齿吞帧、停机只走时码
    tc = 0.0
    for f in frames:
        acts = act_by_frame.get(f["frame"], [])
        dt = 1.0 / FPS
        skip = False
        resume = None
        for a in acts:
            if a["type"] == ACTION_SLOW:
                dt *= a.get("factor", 2.0)
            elif a["type"] == ACTION_SKIP:
                skip = True
            elif a["type"] == ACTION_HOLD:
                f["hold"] = True
                if a.get("resume_tc") is not None:
                    resume = a["resume_tc"]
        f["tc"] = tc
        f["skipped"] = skip
        tc = resume if resume is not None else tc + dt

    # 首个失步位置：偏移量首次越过牵引上限
    first_slip = None
    for f in frames:
        if abs(f["offset"]) > traction:
            first_slip = {"frame": f["frame"], "x": f["x"],
                          "offset": f["offset"]}
            break

    # 锚点续算：anchor 之前用缓存帧，之后以 anchor 为零点重算
    if anchor_id is not None and prefix:
        cut = next((i for i, f in enumerate(frames)
                    if f["sprocket_id"] == anchor_id), None)
        if cut is not None and cut > 0:
            base = frames[cut]["offset"]
            for f in frames[cut:]:
                f["offset"] -= base
            frames = prefix[:cut] + frames[cut:]

    return frames, first_slip


def validate_lock(params, measurements, actions):
    """锁定前校验。返回错误列表，空列表表示允许锁定。"""
    errors = []
    nominal = params["nominal_pitch"]
    sprockets = sorted_sprockets(measurements)

    # 1. 编号回退：录入序号必须随条带位置单调不减
    by_seq = sorted(sprockets, key=lambda m: m["seq"])
    for a, b in zip(by_seq, by_seq[1:]):
        if b["x"] < a["x"] - 1e-9:
            errors.append("编号回退：序号 %d 的位置 %.2f 早于序号 %d 的 %.2f"
                          % (b["seq"], b["x"], a["seq"], a["x"]))
            break

    # 2. 接片两侧无法唯一对齐：接片任一侧不足两个齿孔
    for sp in markers(measurements, "splice"):
        left = [m for m in sprockets if m["x"] < sp["x"]]
        right = [m for m in sprockets if m["x"] > sp["x"]]
        if len(left) < 2 or len(right) < 2:
            errors.append("接片 x=%.2f 两侧齿孔不足（左 %d / 右 %d），"
                          "无法唯一对齐" % (sp["x"], len(left), len(right)))

    # 3. 缺测跨度太长
    for a, b in zip(sprockets, sprockets[1:]):
        gap = b["x"] - a["x"]
        if gap > MAX_GAP_PITCHES * nominal:
            errors.append("缺测跨度 %.2fmm 超过上限 %d 个节距"
                          % (gap, MAX_GAP_PITCHES))
            break

    # 4. 托带动作跨过脆裂边
    brittle = markers(measurements, "brittle")
    if brittle and sprockets:
        x0 = sprockets[0]["x"]
        for a in actions:
            if a["type"] != ACTION_HOLD:
                continue
            span = a.get("span", 1)
            xa = x0 + a["frame_index"] * nominal
            xb = xa + span * nominal
            for br in brittle:
                if xa < br["x"] < xb:
                    errors.append("托带动作（帧 %d，跨 %d 帧）跨过脆裂边 x=%.2f"
                                  % (a["frame_index"], span, br["x"]))

    # 5. 校正时码重叠
    frames, _ = build_frames(params, measurements, actions)
    tcs = [f["tc"] for f in frames if not f.get("skipped")]
    if any(b <= a for a, b in zip(tcs, tcs[1:])):
        errors.append("校正时码重叠：存在非递增时码")

    return errors
