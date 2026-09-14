"""走带编排计算核心。

JSON API、偏移 SVG、走带卡共用这里的函数，保证同一版参数得到同一份结果。
所有长度单位与录入一致（毫米），时间在内部以秒计。

双边门位：逐帧配对左右齿孔/片边观测，逐帧求片宽、中心线横移、画格旋角，
关键帧之间线性插值出连续门位补偿，并核算扫描窗对画面的安全裁切余量。
"""

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
