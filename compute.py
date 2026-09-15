"""走带编排计算核心。

JSON API、偏移 SVG、走带卡共用这里的函数，保证同一版参数得到同一份结果。
所有长度单位与录入一致（毫米），时间在内部以秒计。

双边门位：逐帧配对左右齿孔/片边观测，逐帧求片宽、中心线横移、画格旋角，
关键帧之间线性插值出连续门位补偿，并核算扫描窗对画面的安全裁切余量。
"""

import copy
import hashlib
import math

FPS = 24.0                 # 基准片门速率
BASE_MESH_MM = 0.30        # 标称节距下的啮合深度
MESH_TOLERANCE_MM = 0.25   # 节距误差全部吃掉啮合深度的容差
MAX_GAP_PITCHES = 8        # 缺测跨度上限（以标称节距计）

MAX_SIDE_GAP_FRAMES = 4    # 单侧缺测连续帧数上限
WIDTH_JUMP_MM = 0.50       # 相邻配对面的片宽突变阈值
COMP_JUMP_SHIFT_MM = 0.50  # 门位补偿相邻帧横移跳变阈值
COMP_JUMP_ANGLE_DEG = 1.0  # 门位补偿相邻帧旋角跳变阈值

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

    # 锚点续算：先按当前参数建立全段一致基准，再以锚点为零点续算后方。
    # 版本匹配时锚点前沿直接取缓存前缀；版本已变则用本次全段重算的前段
    # （同为当前参数基准），锚点当次即生效。
    if anchor_id is not None:
        cut = next((i for i, f in enumerate(frames)
                    if f["sprocket_id"] == anchor_id), None)
        if cut is not None and cut > 0:
            base = frames[cut]["offset"]
            for f in frames[cut:]:
                f["offset"] -= base
            if prefix:
                frames = prefix[:cut] + frames[cut:]

    # 首个失步位置：偏移量首次越过牵引上限（在最终帧列上判定）
    first_slip = None
    for f in frames:
        if abs(f["offset"]) > traction:
            first_slip = {"frame": f["frame"], "x": f["x"],
                          "offset": f["offset"]}
            break

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

    # 4. 托带动作跨过脆裂边：按相邻齿孔的实测坐标界定托带跨度
    frames, _ = build_frames(params, measurements, actions)
    brittle = markers(measurements, "brittle")
    if brittle and frames:
        for a in actions:
            if a["type"] != ACTION_HOLD:
                continue
            i0 = a["frame_index"]
            if i0 >= len(frames):
                continue
            span = a.get("span", 1)
            xa = frames[i0]["x"]                 # 托带起点：实测齿孔位
            end = i0 + span
            if end < len(frames):
                xb = frames[end]["x"]            # 终点：下一实测齿孔位
            else:
                xb = frames[-1]["x"] + frames[-1]["pitch"]
            for br in brittle:
                if xa <= br["x"] <= xb:
                    errors.append("托带动作（帧 %d，跨 %d 帧）实测跨度 "
                                  "%.2f–%.2fmm 跨过脆裂边 x=%.2f"
                                  % (a["frame_index"], span, xa, xb, br["x"]))

    # 5. 校正时码重叠
    tcs = [f["tc"] for f in frames if not f.get("skipped")]
    if any(b <= a for a, b in zip(tcs, tcs[1:])):
        errors.append("校正时码重叠：存在非递增时码")

    return errors


# ========== 双边门位稳定 ==========
#
# 坐标约定（毫米）：x 沿片长（走片方向，与条带图一致），y 横跨片宽；
# 左右侧以齿孔条带区分，y 为相对各自侧的横移量（向片心为正）。
# 片宽 = 标称片宽 − 左横移 − 右横移；中心线横移 = (右 − 左)/2。
# 旋角由两侧齿孔中心在 x 方向的错位测得：angle = atan2(右x−左x, 片宽)。
#
# 扫描窗：跨片宽方向半幅取标称片宽/2（收缩后实测片宽更窄，两侧多出的
# 夹持量即天然余量），再从其中预留 safe_margin 的净空作为允许下限；
# 沿片长方向半幅 = 窗口尺寸/2。窗口可整体横移 window_offset。

SIDES = ("L", "R")


def _measure_pair(params, edges, fi):
    """构造一帧的测量量（齿孔/片边分别取该侧该帧的可用观测）。"""
    def pick(side, kind):
        cand = [e for e in edges if e["frame_index"] == fi
                and e["side"] == side and e.get("kind") == kind
                and e.get("usable", 1)]
        return cand[0] if len(cand) == 1 else None

    ls, rs = pick("L", "sprocket"), pick("R", "sprocket")
    le, re_ = pick("L", "edge"), pick("R", "edge")
    p = {"frame": fi, "usable": bool(ls or rs or le or re_), "paired": False,
         "gauge": None, "width": None, "center": None,
         "skew": None, "angle": None,
         "has_left": bool(ls or le), "has_right": bool(rs or re_),
         "left": ls or le, "right": rs or re_}

    # 片宽与中心线：两侧片边观测（y 为该侧相对标称边位向片心的收缩量）
    if le and re_ and le["y"] is not None and re_["y"] is not None:
        p["width"] = params["film_width"] - le["y"] - re_["y"]
        p["center"] = (re_["y"] - le["y"]) / 2.0
        p["paired"] = True
    # 旋角与跨距：两侧齿孔中心（x 沿片长，y 横移）
    if ls and rs and ls["x"] is not None and rs["x"] is not None:
        gauge = params["film_width"] - (ls["y"] or 0.0) - (rs["y"] or 0.0)
        p["gauge"] = gauge
        p["skew"] = rs["x"] - ls["x"]
        if gauge > 1e-9:
            p["angle"] = math.degrees(math.atan2(rs["x"] - ls["x"], gauge))
        p["paired"] = True
        if p["center"] is None and ls["y"] is not None and rs["y"] is not None:
            p["center"] = (rs["y"] - ls["y"]) / 2.0
    return p


def measure_frames(params, edges):
    """逐帧配对：返回 (meas, ambiguous)。

    meas 按帧号排列，含成对帧与单侧帧（paired=False）；
    ambiguous 为同帧同侧有多条可用观测的帧（配对多解）。
    """
    frame_ids = sorted({e["frame_index"] for e in edges})
    meas, ambiguous = [], []
    for fi in frame_ids:
        multi = False
        for side in SIDES:
            for kind in ("sprocket", "edge"):
                n = sum(1 for e in edges if e["frame_index"] == fi
                        and e["side"] == side and e["kind"] == kind
                        and e.get("usable", 1))
                if n > 1:
                    multi = True
        if multi:
            ambiguous.append(fi)
        meas.append(_measure_pair(params, edges, fi))
    return meas, ambiguous


def _angle_lerp(a0, a1, t):
    """最短弧插值，避免 ±180° 跳变。"""
    d = (a1 - a0 + 180.0) % 360.0 - 180.0
    return a0 + d * t


def _interp_meas(meas, fi):
    """测量量按帧线性插值（片段外夹取端点值）；无测量时返回 None。"""
    if not meas:
        return None
    if fi <= meas[0]["frame"]:
        m = dict(meas[0])
        m["frame"] = fi
        return m
    if fi >= meas[-1]["frame"]:
        m = dict(meas[-1])
        m["frame"] = fi
        return m
    for a, b in zip(meas, meas[1:]):
        if a["frame"] <= fi <= b["frame"]:
            t = (fi - a["frame"]) / max(1, b["frame"] - a["frame"])
            m = {"frame": fi, "usable": True, "paired": True}
            for k in ("gauge", "width", "center", "skew"):
                va, vb = a.get(k), b.get(k)
                m[k] = (va + (vb - va) * t) if va is not None and vb is not None \
                    else (va if va is not None else vb)
            if a.get("angle") is not None and b.get("angle") is not None:
                m["angle"] = _angle_lerp(a["angle"], b["angle"], t)
            else:
                m["angle"] = a.get("angle") if a.get("angle") is not None \
                    else b.get("angle")
            return m
    return None


def observation_signature(edges):
    """双边观测指纹：观测改动即失效全部门位区间缓存。"""
    h = hashlib.sha1()
    for e in sorted(edges, key=lambda x: x["id"]):
        h.update(("%(id)d %(frame_index)d %(side)s %(kind)s %(x)s %(y)s "
                  "%(usable)d" % {
                      "id": e["id"], "frame_index": e["frame_index"],
                      "side": e["side"], "kind": e["kind"],
                      "x": e.get("x"), "y": e.get("y"),
                      "usable": e.get("usable", 1)}).encode())
    return h.hexdigest()[:16]


def _key_signature(kf):
    return "%d:%.4f:%.4f" % (kf["frame_index"], kf["shift"], kf["angle"])


def _zones(keyframes, n_frames):
    """按关键帧切区间。返回 [(key, f0, f1, kf0, kf1)]，帧号闭区间。

    无关键帧时整段为一个零补偿区间；有关键帧时首尾为夹取区间，
    中间为线性插值区间。
    """
    kfs = sorted(keyframes, key=lambda k: k["frame_index"])
    if not kfs:
        return [("zone:none", 0, max(0, n_frames - 1), None, None)]
    zones = []
    first = kfs[0]
    if first["frame_index"] > 0:
        zones.append(("zone:head:%d" % first["frame_index"],
                      0, first["frame_index"] - 1, first, None))
    for a, b in zip(kfs, kfs[1:]):
        zones.append(("zone:%d-%d" % (a["frame_index"], b["frame_index"]),
                      a["frame_index"], b["frame_index"], a, b))
    last = kfs[-1]
    if last["frame_index"] < n_frames - 1:
        zones.append(("zone:tail:%d" % last["frame_index"],
                      last["frame_index"] + 1, n_frames - 1, last, None))
    return zones


def _frame_margins(params, m, shift, angle):
    """扫描窗相对画格的四边安全裁切余量（mm，负值即侵入画面）。

    画格半幅：跨片宽 = 实测片宽/2，沿片长 = 窗口尺寸/2；
    扫描窗跨片宽半幅 = 标称片宽/2（窗覆盖到片边）；沿片长方向窗在
    标称画格外再外扩 safe_margin 的安全夹持量（overscan）。
    补偿后残差：横移 center−shift−window_offset、旋角 meas_angle−angle。
    旋角残差使画格两轴投影半幅增大（|cos|/|sin| 项）。跨片宽方向收缩使
    片变窄、天然多出夹持量，再扣 safe_margin 净空；沿片长方向靠外扩量
    吸收旋角投影。任一净余量为负即画面侵入扫描窗边缘（被裁）。
    """
    pic_across = (m["width"] if m.get("width") is not None
                  else params["film_width"]) / 2.0
    pic_along = params["window_size"] / 2.0
    win_across = params["film_width"] / 2.0
    win_along = params["window_size"] / 2.0 + params["safe_margin"]
    center = m.get("center") or 0.0
    res_angle = (m.get("angle") or 0.0) - angle
    t = math.radians(res_angle)
    c, s = abs(math.cos(t)), abs(math.sin(t))
    dx = center - shift - params["window_offset"]
    across = win_across - (c * pic_across + s * pic_along) - abs(dx) \
        - params["safe_margin"]
    along = win_along - (c * pic_along + s * pic_across)
    return {"across": across, "along": along,
            "min": min(across, along)}


def build_gate(params, edges, keyframes, n_frames, cache=None):
    """在关键帧之间生成连续门位补偿。

    cache 为上次的门位缓存（dict）；只在参数版本、观测指纹、区间端点
    关键帧签名三者一致时复用区间结果，否则只重算受影响区间。
    返回 dict：frames / keyframes / recomputed_zones / obs_sig /
    pair_count / version。
    """
    meas, ambiguous = measure_frames(params, edges)
    kfs = sorted(keyframes, key=lambda k: k["frame_index"])
    obs_sig = observation_signature(edges)
    version = params["version"]

    cached = {}
    if (isinstance(cache, dict) and cache.get("version") == version
            and cache.get("obs_sig") == obs_sig):
        cached = {z["key"]: z for z in cache.get("zones", [])}

    zones = _zones(kfs, max(n_frames, 1))
    active = {z[0] for z in zones}
    # 区间集合可能因插入/删除关键帧而变化：丢弃已不存在的旧区间
    for stale in [k for k in cached if k not in active]:
        del cached[stale]

    recomputed = []
    frames = {}
    for key, f0, f1, k0, k1 in zones:
        zc = cached.get(key)
        if (zc is not None and zc.get("sig0") == (
                _key_signature(k0) if k0 else None)
                and zc.get("sig1") == (
                    _key_signature(k1) if k1 else None)):
            for row in zc["rows"]:
                frames[row["frame"]] = row
            continue
        recomputed.append(key)
        rows = []
        for fi in range(f0, f1 + 1):
            m = _interp_meas(meas, fi)
            if k0 is None:
                shift, angle = 0.0, 0.0
            elif k1 is None:
                shift, angle = k0["shift"], k0["angle"]
            else:
                t = 0.0 if k1["frame_index"] == k0["frame_index"] else \
                    (fi - k0["frame_index"]) / (k1["frame_index"]
                                                - k0["frame_index"])
                shift = k0["shift"] + (k1["shift"] - k0["shift"]) * t
                angle = _angle_lerp(k0["angle"], k1["angle"], t)
            row = {"frame": fi, "shift": shift, "angle": angle,
                   "meas_center": (m.get("center") if m else None),
                   "meas_width": (m.get("width") if m else None),
                   "meas_angle": (m.get("angle") if m else None)}
            if m is not None:
                row.update(_frame_margins(params, m, shift, angle))
            else:
                row.update({"across": None, "along": None, "min": None})
            rows.append(row)
            frames[fi] = row
        # 回写新缓存（调用方持久化）
        cached[key] = {"key": key,
                       "sig0": _key_signature(k0) if k0 else None,
                       "sig1": _key_signature(k1) if k1 else None,
                       "rows": rows}

    out_frames = [frames[i] for i in sorted(frames)]
    new_cache = {"version": version, "obs_sig": obs_sig,
                 "zones": [cached[k] for k in sorted(cached)]}
    return {
        "version": version,
        "obs_sig": obs_sig,
        "ambiguous_frames": ambiguous,
        "keyframes": [dict(k) for k in kfs],
        "frames": out_frames,
        "recomputed_zones": recomputed,
        "pair_count": sum(1 for m in meas if m["paired"]),
        "measured_frames": meas,
        "cache": new_cache,
    }


def default_keyframe(params, edges, frame_index):
    """新关键帧默认贴住该帧的实测门位（横移、旋角）。"""
    m = _interp_meas(measure_frames(params, edges)[0], frame_index)
    return {"frame_index": frame_index,
            "shift": (m.get("center") or 0.0) if m else 0.0,
            "angle": (m.get("angle") or 0.0) if m else 0.0}


def validate_gate(params, edges, keyframes, gate):
    """双边门位锁定校验。返回错误列表（空列表放行），每条都带首帧定位。"""
    errors = []
    meas, ambiguous = measure_frames(params, edges)
    by_frame = {m["frame"]: m for m in meas}

    # 1. 配对多解：首帧
    if ambiguous:
        fi = ambiguous[0]
        errors.append("门位：帧 %d 单侧存在多条可用齿孔/片边观测，配对多解，"
                      "请删除或标缺" % fi)

    # 2. 单侧缺测过长：只统计有观测覆盖的帧范围；双侧都缺只是无数据，
    # 一侧在、另一侧连续缺测超过上限才报错。
    if meas:
        f_lo, f_hi = meas[0]["frame"], meas[-1]["frame"]
        run, run_start, worst, worst_start = 0, None, 0, None
        for fi in range(f_lo, f_hi + 1):
            m = by_frame.get(fi)
            one_side = bool(m and m["usable"] and not m["paired"])
            if one_side:
                if run == 0:
                    run_start = fi
                run += 1
                if run > worst:
                    worst, worst_start = run, run_start
            else:
                run = 0
        if worst > MAX_SIDE_GAP_FRAMES:
            m = by_frame.get(worst_start)
            side = "右" if (m and m.get("has_left")
                            and not m.get("has_right")) else "左"
            errors.append("门位：帧 %d 起单侧（%s）连续缺测 %d 帧，超过上限 %d 帧"
                          % (worst_start, side, worst, MAX_SIDE_GAP_FRAMES))

    # 3. 片宽突变：相邻配对面片宽差超阈值
    prev = None
    for m in meas:
        if not m["paired"] or m.get("width") is None:
            continue
        if prev is not None and abs(m["width"] - prev["width"]) > WIDTH_JUMP_MM:
            errors.append("门位：帧 %d 片宽突变 %.2f→%.2fmm（Δ %.2f > %.2f），"
                          "疑为片边误标或撕裂"
                          % (m["frame"], prev["width"], m["width"],
                             abs(m["width"] - prev["width"]), WIDTH_JUMP_MM))
            break
        prev = m

    # 4. 补偿跳变：相邻帧补偿横移/旋角差超阈值
    frames = gate["frames"]
    for a, b in zip(frames, frames[1:]):
        ds = abs(b["shift"] - a["shift"])
        da = abs((b["angle"] - a["angle"] + 180.0) % 360.0 - 180.0)
        if ds > COMP_JUMP_SHIFT_MM or da > COMP_JUMP_ANGLE_DEG:
            errors.append("门位：帧 %d→%d 补偿跳变（Δshift %.2fmm / "
                          "Δangle %.2f°），关键帧间距过大或数值误置"
                          % (a["frame"], b["frame"], ds, da))
            break

    # 5. 裁切侵入画面：任一边余量为负
    for f in frames:
        if f.get("min") is not None and f["min"] < -1e-9:
            errors.append("门位：帧 %d 扫描窗侵入画面（最小余量 %.3fmm），"
                          "横移 %.2f / 旋角 %.2f° 未补偿到位"
                          % (f["frame"], f["min"], f["shift"], f["angle"]))
            break

    return errors


# ========== 焦面排程 ==========
#
# 受潮胶片横向拱起后，同一画格中央与四角落在不同物距上；门位补偿只能稳住
# 构图，局部仍失焦。修复师在 Canvas 画格上记录五个标准点位的测高（中央 C、
# 四角 TL/TR/BL/BR）以及接片前后的基准点（SB/SA），后台对每个点位沿时间
# 线性插值，拟合出每帧的翘曲包络 [z_near, z_far]，再按三种策略排出逐帧
# 焦位与电机速度：
#
#   constant    恒定：全段一个焦位（首锚焦位；无锚取包络总中值）
#   recommended 推荐：每帧咬住包络中点，电机按时间尺可达即跟随
#   manual      人工：焦点锚点之间线性插值，锚点外夹取端点值
#
# 景深窗 [focus−dof/2, focus+dof/2] 覆盖到的测点比例即清晰覆盖比例。
# 电机可行性：相邻曝光帧之间 |Δfocus| ≤ speed·(dt − settle)；hold/skip/
# 降速都在帧时间尺 dt 里，天然给电机更多时间。
#
# 与门位一样按区间缓存：人工策略夹在相邻锚点之间，改一个锚点只重算两侧
# 区间；恒定/推荐为整段单区间。测高稿指纹（h_sig）、策略、镜头参数版本
# 三者一致才复用。焦域热图、重演 JSON、走带卡固定同一 h_sig 与策略版本。

FOCUS_CANON = ("C", "TL", "TR", "BL", "BR")
FOCUS_SPLICE_PRE = "SB"          # 接片前基准
FOCUS_SPLICE_POST = "SA"         # 接片后基准
FOCUS_STRATEGIES = ("constant", "recommended", "manual")
FOCUS_STRATEGY_CN = {
    "constant": "恒定", "recommended": "推荐", "manual": "人工"}

MAX_HEIGHT_GAP_FRAMES = 6        # 无测高帧连续跨度上限
MIN_FOCUS_COVERAGE = 0.80        # 清晰覆盖比例下限


def _usable_heights(heights):
    return [h for h in heights if h.get("usable", 1)
            and h.get("z") is not None]


def height_signature(heights):
    """测高稿指纹：测高增删改（含标缺）即失效全部焦面区间缓存。"""
    h = hashlib.sha1()
    for e in sorted(heights, key=lambda x: x["id"]):
        h.update(("%(id)d %(frame_index)d %(pos)s %(z)s %(usable)d" % {
            "id": e["id"], "frame_index": e["frame_index"],
            "pos": e["pos"], "z": e.get("z"),
            "usable": e.get("usable", 1)}).encode())
    return h.hexdigest()[:16]


def _height_at(usable, fi, pos):
    """某个点位在 fi 帧的物距：同帧多点位取均值（多解另由校验拦截），
    帧间线性插值，片段外夹取端点值；该点位从无观测返回 None。"""
    pts = sorted((h for h in usable if h["frame_index"] is not None
                  and h["pos"] == pos), key=lambda h: h["frame_index"])
    if not pts:
        return None
    here = [h["z"] for h in pts if h["frame_index"] == fi]
    if here:
        return sum(here) / len(here)
    if fi <= pts[0]["frame_index"]:
        return pts[0]["z"]
    if fi >= pts[-1]["frame_index"]:
        return pts[-1]["z"]
    for a, b in zip(pts, pts[1:]):
        if a["frame_index"] < fi < b["frame_index"]:
            t = (fi - a["frame_index"]) / (b["frame_index"] - a["frame_index"])
            return a["z"] + (b["z"] - a["z"]) * t
    return None


def focus_envelope(params, usable, fi):
    """fi 帧翘曲包络：返回 (near, far, by_pos)，无任何标准点位时为 None。"""
    by_pos = {p: _height_at(usable, fi, p) for p in FOCUS_CANON}
    vals = [v for v in by_pos.values() if v is not None]
    if not vals:
        return None
    return min(vals), max(vals), by_pos


def _focus_zone_keys(anchors, n_frames):
    """人工策略按锚点切区间（沿用门位区间定义）；无锚/恒定/推荐为整段。"""
    kfs = sorted(anchors, key=lambda k: k["frame_index"])
    return _zones(kfs, max(n_frames, 1))


def _anchor_signature(a):
    return "%d:%.5f" % (a["frame_index"], a["focus"])


def default_focus_anchor(params, usable, fi):
    """新焦点锚点默认咬住该帧包络中点（推荐起点）；无包络取焦程中点。"""
    env = focus_envelope(params, usable, fi)
    if env:
        focus = (env[0] + env[1]) / 2.0
    else:
        focus = (params["focus_near"] + params["focus_far"]) / 2.0
    lo, hi = sorted((params["focus_near"], params["focus_far"]))
    return {"frame_index": fi, "focus": min(hi, max(lo, focus))}


def _frame_t(frames, fi):
    f = next((x for x in frames if x["frame"] == fi), None)
    return f["tc"] if f else fi / FPS


def build_focus(params, heights, anchors, frames, strategy, cache=None,
                n_frames=None):
    """焦面排程主函数。返回逐帧焦位/速度/覆盖率与区间缓存（同 build_gate）。

    每行：frame / z_near / z_far / mid / target / focus / speed /
          dt / settled（机构是否来得及稳定）/ in_range / coverage /
          skipped / strategy。
    """
    if strategy not in FOCUS_STRATEGIES:
        strategy = "constant"
    usable = _usable_heights(heights)
    h_sig = height_signature(heights)
    version = params["version"]
    dof = params["lens_dof"]
    speed_max = params["motor_speed"]
    settle = params["settle_time"]
    f_lo, f_hi = sorted((params["focus_near"], params["focus_far"]))
    if n_frames is None:
        n_frames = len(frames)
    n_frames = max(n_frames, 1)

    # 缓存命中条件：镜头参数版本、测高稿指纹、区间端点签名一致即复用包络。
    # 缓存体只存与策略无关的翘曲包络；目标焦位/速度/覆盖每次按当前策略
    # 重算，切换策略后首帧即得到正确目标（不把目标带进缓存串味）。
    cached = {}
    if (isinstance(cache, dict) and cache.get("version") == version
            and cache.get("h_sig") == h_sig):
        cached = {z["key"]: z for z in cache.get("zones", [])}

    if strategy == "manual":
        raw_zones = _focus_zone_keys(anchors, n_frames)
    else:
        raw_zones = [("zone:%s:all" % strategy, 0, n_frames - 1, None, None)]
    # 区间键带策略前缀，避免人工区间与恒焦/推荐整段键在跨策略复用缓存时撞名。
    # 元组约定与门位 _zones 一致：head=(first,None) 夹首锚，tail=(last,None)
    # 夹尾锚，中间 (a,b) 线性插值。
    zones = [("%s:%s" % (strategy, key), f0, f1, ka, kb)
             for key, f0, f1, ka, kb in raw_zones]
    active = {z[0] for z in zones}
    for stale in [k for k in cached if k not in active]:
        del cached[stale]

    recomputed, rowmap = [], {}

    def envelope_rows(f0, f1):
        rows = []
        for fi in range(f0, f1 + 1):
            env = focus_envelope(params, usable, fi)
            if env is None:
                rows.append({"frame": fi, "z_near": None, "z_far": None,
                             "mid": None, "by_pos": None})
            else:
                rows.append({"frame": fi, "z_near": env[0], "z_far": env[1],
                             "mid": (env[0] + env[1]) / 2.0,
                             "by_pos": env[2]})
        return rows

    # 第一遍：区间包络（只在这里读写缓存），顺便汇总全段中值
    mids = []
    for key, f0, f1, k0, k1 in zones:
        sig0 = _anchor_signature(k0) if k0 else None
        sig1 = _anchor_signature(k1) if k1 else None
        zc = cached.get(key)
        if zc is None or zc.get("sig0") != sig0 or zc.get("sig1") != sig1:
            recomputed.append(key)
            zc = {"key": key, "sig0": sig0, "sig1": sig1,
                  "rows": envelope_rows(f0, f1)}
            cached[key] = zc
        mids.extend(r["mid"] for r in zc["rows"] if r["mid"] is not None)

    # 恒焦值：首锚焦位；无锚取包络总中值
    if strategy == "constant":
        if anchors:
            const_focus = sorted(anchors,
                                 key=lambda a: a["frame_index"])[0]["focus"]
        elif mids:
            const_focus = (min(mids) + max(mids)) / 2.0
        else:
            const_focus = (f_lo + f_hi) / 2.0

    # 第二遍：按当前策略生成目标焦位（输出行独立于缓存，不污染包络缓存）。
    # 旧缓存行里可能残留上一轮的 target/focus 字段，先裁回纯包络四字段。
    envelope_keys = ("frame", "z_near", "z_far", "mid", "by_pos")
    for key, f0, f1, k0, k1 in zones:
        for src in cached[key]["rows"]:
            r = {k: copy.deepcopy(src.get(k)) for k in envelope_keys}
            fi = r["frame"]
            if strategy == "recommended":
                target = r["mid"]
            elif strategy == "constant":
                target = const_focus
            else:  # manual：约定同门位 _zones——head 元组 (first,None)
                # 夹首锚；tail (last,None) 夹尾锚；中间 (a,b) 线性插值；
                # 无任何锚点时退化为调焦范围中值（校验会提示人工策略需加锚）
                if k0 is None and k1 is None:
                    target = (f_lo + f_hi) / 2.0
                elif k1 is None:
                    target = k0["focus"]
                else:
                    t = 0.0 if k1["frame_index"] == k0["frame_index"] else \
                        (fi - k0["frame_index"]) / (k1["frame_index"]
                                                    - k0["frame_index"])
                    target = k0["focus"] + (k1["focus"] - k0["focus"]) * t
            r["target"] = target
            r["strategy"] = strategy
            rowmap[fi] = r

    # 电机排程：沿校正时间尺求速度与可达性，再按实际下达焦位算清晰覆盖。
    # 目标先不裁剪，超焦程由校验按首帧拦截（焦位由机构限位夹住，
    # 覆盖仍按夹住后的焦位核算）。无包络帧不产生焦位需求，其时间也
    # 可供电机继续移动，因此用上一个有目标帧作为移动起点。
    prev_fi = None
    prev_target = None
    for fi in sorted(rowmap):
        r = rowmap[fi]
        t = _frame_t(frames, fi)
        skipped = any(f.get("skipped") for f in frames if f["frame"] == fi)
        r["skipped"] = skipped
        if prev_fi is None:
            dt, move_need = None, 0.0
        else:
            dt = max(0.0, t - _frame_t(frames, prev_fi))
            move_need = (abs(r["target"] - prev_target)
                         if r["target"] is not None and prev_target is not None
                         else 0.0)
        r["dt"] = dt
        r["in_range"] = (r["target"] is None
                         or (f_lo - 1e-9 <= r["target"] <= f_hi + 1e-9))
        r["focus"] = (min(f_hi, max(f_lo, r["target"]))
                      if r["target"] is not None else None)
        if dt is None:
            r["speed"] = 0.0
            r["settled"] = True
        else:
            r["speed"] = move_need / dt if dt > 1e-12 else float("inf")
            r["settled"] = (dt >= settle
                            and move_need <= speed_max * max(0.0, dt - settle)
                            + 1e-9)
        # 清晰覆盖：景深窗包住的标准测点比例（空齿帧不曝光、不参与）
        if r["by_pos"] and r["focus"] is not None and not skipped:
            vals = [v for v in r["by_pos"].values() if v is not None]
            hit = sum(1 for z in vals
                      if abs(z - r["focus"]) <= dof / 2.0 + 1e-9)
            r["coverage"] = hit / len(vals)
        else:
            r["coverage"] = None
        prev_fi = fi
        if r["target"] is not None:
            prev_target = r["target"]

    out = [rowmap[i] for i in sorted(rowmap)]
    new_cache = {"version": version, "h_sig": h_sig, "strategy": strategy,
                 "zones": [cached[k] for k in sorted(cached)]}
    # 归帧歧义：同帧同标准点位多条可用测高
    ambiguous = sorted({h["frame_index"] for h in usable
                        if h["pos"] in FOCUS_CANON
                        and sum(1 for g in usable
                                if g["frame_index"] == h["frame_index"]
                                and g["pos"] == h["pos"]) > 1})
    return {
        "version": version,
        "h_sig": h_sig,
        "strategy": strategy,
        "anchors": [dict(a) for a in sorted(anchors,
                                            key=lambda a: a["frame_index"])],
        "frames": out,
        "recomputed_zones": recomputed,
        "ambiguous_frames": ambiguous,
        "cache": new_cache,
    }


def validate_focus(params, heights, anchors, frames, focus,
                   splice_frames=(), n_frames=None):
    """焦面排程锁定校验。返回错误列表（空列表放行），每条带首帧定位。

    五类拦截（均停在首个受影响画格）：
    测高归帧歧义 / 接片基准断裂 / 数据空档过长 / 焦域覆盖不足 /
    机构来不及稳定（含焦位超出调焦范围）。
    无测高稿时整项跳过（与门位无观测一致），不对旧段产生新约束。
    splice_frames：条带圈记的接片所在帧号（接片基准也必须落在这些帧上）。
    """
    usable = _usable_heights(heights)
    canon = [h for h in usable if h["pos"] in FOCUS_CANON]
    errors = []
    if not canon and not any(h.get("usable", 1) for h in heights):
        return errors
    if n_frames is None:
        n_frames = len(frames)
    n_frames = max(n_frames, 1)

    # 0. 人工策略必须先布焦点锚点（否则区间无端点可插值）
    if focus["strategy"] == "manual" and not anchors:
        errors.append("焦面：人工策略未布置任何焦点锚点，请在时间轴上"
                      "插入锚点或改用推荐/恒定策略")

    # 1. 测高点归帧歧义：同帧同标准点位多条可用
    if focus["ambiguous_frames"]:
        fi = focus["ambiguous_frames"][0]
        errors.append("焦面：帧 %d 同一点位存在多条可用测高，归帧有歧义，"
                      "请删除或标缺多余测点" % fi)

    # 2. 接片基准断裂：接片两侧都要有基准（SB 接片前 / SA 接片后），
    #    且前后物距跳变不得超过一个景深（跳得过大说明两本片子物距基准
    #    对不上，不能跨接片沿用同一条焦面轨迹）。测高稿上 SB/SA 直接成对，
    #    条带圈记的接片（splice_frames 帧号）则要求该帧有 SB、次帧有 SA。
    dof = params["lens_dof"]
    pre_by_fi = {}
    post_by_fi = {}
    for h in usable:
        if h["pos"] == FOCUS_SPLICE_PRE:
            pre_by_fi.setdefault(h["frame_index"], []).append(h["z"])
        elif h["pos"] == FOCUS_SPLICE_POST:
            post_by_fi.setdefault(h["frame_index"], []).append(h["z"])

    def require_pair(sf):
        """sf 帧为接片：SB 在 sf、SA 在 sf 或 sf+1。返回错误串或 None。"""
        pre = pre_by_fi.get(sf)
        post = post_by_fi.get(sf)
        if post is None:
            post = post_by_fi.get(sf + 1)
        if not pre:
            return ("焦面：帧 %d 接片前缺基准点 SB，接片基准断裂，"
                    "不能跨接片沿用焦位" % sf)
        if not post:
            return ("焦面：帧 %d 接片后缺基准点 SA，接片基准断裂，"
                    "不能跨接片沿用焦位" % sf)
        zpre, zpost = sum(pre) / len(pre), sum(post) / len(post)
        if abs(zpost - zpre) > dof + 1e-9:
            return ("焦面：帧 %d 接片前后物距跳变 %.3fmm 超过景深 %.2fmm，"
                    "接片基准断裂" % (sf, abs(zpost - zpre), dof))
        return None

    pairs = set(pre_by_fi) | set(splice_frames)
    for sf in sorted(pairs):
        msg = require_pair(int(sf))
        if msg:
            errors.append(msg)
            break

    # 3. 数据空档过长：有测高覆盖的帧范围内，连续无直接标准测高的帧数超
    #    上限（注意不能看插值包络——空档恰恰会被插值填上）
    observed = {h["frame_index"] for h in canon}
    if observed:
        lo, hi = min(observed), max(observed)
        run, start = 0, None
        for fi in range(lo, hi + 1):
            if fi not in observed:
                if run == 0:
                    start = fi
                run += 1
                if run > MAX_HEIGHT_GAP_FRAMES:
                    errors.append("焦面：帧 %d 起连续 %d 帧无测高，数据空档"
                                  "超过上限 %d 帧"
                                  % (start, run, MAX_HEIGHT_GAP_FRAMES))
                    break
            else:
                run = 0

    # 4. 焦域覆盖不足：曝光帧清晰覆盖比例低于下限
    for r in focus["frames"]:
        if r["coverage"] is not None and r["coverage"] < MIN_FOCUS_COVERAGE:
            errors.append("焦面：帧 %d 清晰覆盖比例 %.0f%% 低于下限 %.0f%%，"
                          "焦域覆盖不足（景深 %.2fmm 包不住翘曲包络 "
                          "%.3f–%.3fmm）"
                          % (r["frame"], 100 * r["coverage"],
                             100 * MIN_FOCUS_COVERAGE, dof,
                             r["z_near"], r["z_far"]))
            break

    # 5. 机构来不及稳定：焦位超出调焦范围，或相邻曝光帧间速度/静定不满足
    for r in focus["frames"]:
        if r["target"] is None:
            continue
        if not r["in_range"]:
            errors.append("焦面：帧 %d 所需焦位 %.3fmm 超出调焦范围 "
                          "%.3f–%.3fmm，机构无法到达"
                          % (r["frame"], r["target"],
                             params["focus_near"], params["focus_far"]))
            break
        if r["dt"] is not None and not r["settled"]:
            if r["dt"] <= 1e-12:
                errors.append("焦面：帧 %d 校正时码间隔为零，机构无移动"
                              "时间，来不及稳定，请加降速/托带"
                              % r["frame"])
                break
            move = r["speed"] * r["dt"]
            errors.append("焦面：帧 %d 机构来不及稳定（需移动 %.3fmm、"
                          "用时 %.3fs，超出速度 %.2fmm/s 或静定 %.2fs），"
                          "请加降速/托带或人工锚点"
                          % (r["frame"], move, r["dt"],
                             params["motor_speed"], params["settle_time"]))
            break

    return errors
