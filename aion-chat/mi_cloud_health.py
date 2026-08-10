"""小米云端亲友健康数据轮询 → 入库 health_miband_activity + health_ring_heart_rates。

通过 mi-fitness SDK（GitHub: Misty02600/mi-fitness-python）以小号 token
读取亲友（大号）的睡眠/心率/步数。

数据分两路入库：
  1. 每日汇总（get_heart_rate/get_steps/get_sleep，日粒度）→ health_miband_activity，
     source 用 'mi_cloud'，measured_at=当天 0 点，保存实际返回的日均心率、当日总步数、
     睡眠总时长及深睡/浅睡/REM 分钟数；字段缺失保持 0（前端按 precision=daily 判断未知，
     不会把 0 当真实测量）。
  2. 最新快照（get_latest_data，真实采样时间）→ health_ring_heart_rates，
     source 用 'mi_cloud'，raw_json 标注 kind=latest_snapshot + 账号数据时间 + 设备名，
     作为“最新心率”进入前端与 AI 健康上下文；日均值不会冒充实时心率。

前置条件（账号侧，需用户手动）：
  1. 注册小米小号
  2. 小号在「小米运动健康」App 加大号为亲友，大号同意授权（睡眠/心率/步数全勾）
  3. 跑 mi_cloud_login.py 扫码登录小号，生成 data/mi_cloud_token.json
  4. 在 data/settings.json 配 mi_cloud.relative_uid（大号的 UID，可从亲友列表查到）
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from database import get_db
from health_context import analyze_heart_rate_entry, insert_heart_rate

DATA_DIR = Path(__file__).parent / "data"
TOKEN_PATH = DATA_DIR / "mi_cloud_token.json"
SYNC_STATE_PATH = DATA_DIR / "mi_cloud_sync_state.json"
SOURCE = "mi_cloud"
DEVICE_DEFAULT = "Redmi Smart Band 2"

# 轮询间隔（秒）。云端数据本身有延迟，5 分钟足够。
POLL_INTERVAL = 5 * 60

# 首次启动时补拉的历史天数：小米云端睡眠/步数隔天才有完整汇总，
# 不补的话昨天和更早的有效数据永远进不来。
HISTORY_BACKFILL_DAYS = 7

# 进程内同步锁：后台轮询与手动 /api/health/mi-band/cloud-sync 不能同时写库。
_sync_lock = asyncio.Lock()


def _load_settings() -> dict[str, Any]:
    try:
        with open(DATA_DIR / "settings.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_sync_state() -> dict[str, Any]:
    """持久化同步检查点：避免每次重启重拉 7 天历史，并记录失败天供下轮重试。

    结构：{backfilled_through:"YYYY-MM-DD", failed_days:["YYYY-MM-DD"...], last_run_at:float}
    backfilled_through 表示历史已成功补到哪一天；之后每轮只需今天 + failed_days。
    """
    try:
        with open(SYNC_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
            if isinstance(state, dict):
                return state
    except Exception:
        pass
    return {"backfilled_through": None, "failed_days": [], "last_run_at": 0}


def _save_sync_state(state: dict[str, Any]) -> None:
    try:
        with open(SYNC_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _day_iso(day: date) -> str:
    return day.isoformat()


def _day_start_ts(day: date) -> float:
    """当天本地 0 点的时间戳。日汇总行统一用它当 measured_at，三次接口可合并到一行。"""
    return datetime(day.year, day.month, day.day).timestamp()


async def _fetch_relative_uid(client) -> tuple[int | None, str | None]:
    """从 settings 读 relative_uid；未配置则返回 (None, reason) 而不是静默取亲友列表第一个。

    静默取 relatives[0] 会在新增/重排亲友时悄悄同步错人的健康数据，
    没有用户隔离的健康表里混入他人数据且不易察觉，故强制要求显式配置。
    """
    cfg = _load_settings().get("mi_cloud", {})
    uid = cfg.get("relative_uid")
    if uid:
        try:
            return int(uid), None
        except (TypeError, ValueError):
            return None, f"mi_cloud.relative_uid 配置无效：{uid!r}"
    return None, "未配置 mi_cloud.relative_uid，已停止同步避免错人（不再默认取亲友列表第一个）"


async def _persist_sample(
    db: aiosqlite.Connection,
    *,
    measured_at: float,
    device_name: str,
    source: str = SOURCE,
    heart_rate: Optional[int] = None,
    steps: Optional[int] = None,
    sleep_stage: Optional[str] = None,
    sleep: Optional[int] = None,
    deep_sleep: Optional[int] = None,
    rem_sleep: Optional[int] = None,
    calories: Optional[int] = None,
    valid_stand: Optional[int] = None,
    intensity_min: Optional[int] = None,
    sleep_score: Optional[int] = None,
    sleep_awake: Optional[int] = None,
    awake_count: Optional[int] = None,
    sleep_start: Optional[float] = None,
    sleep_end: Optional[float] = None,
    sleep_avg_hr: Optional[int] = None,
    sleep_max_hr: Optional[int] = None,
    now: float,
) -> None:
    # 区分“本次接口没返回该字段”(None -> 保留旧值) 与“返回了 0”(有效测量 -> 覆盖)。
    # 旧实现 COALESCE(NULLIF(excluded,0), old) 把合法 0 当缺失，平台校正为 0 时旧值残留。
    def _val(v) -> int:
        return int(v) if v is not None else 0

    sets = ["device_name=excluded.device_name", "synced_at=excluded.synced_at"]
    if heart_rate is not None:
        sets.append("heart_rate=excluded.heart_rate")
    if steps is not None:
        sets.append("steps=excluded.steps")
    if sleep is not None:
        sets.append("sleep_value=excluded.sleep_value")
    if deep_sleep is not None:
        sets.append("deep_sleep_value=excluded.deep_sleep_value")
    if rem_sleep is not None:
        sets.append("rem_sleep_value=excluded.rem_sleep_value")
    if sleep_stage:
        sets.append("sleep_stage=excluded.sleep_stage")
    if calories is not None:
        sets.append("calories=excluded.calories")
    if valid_stand is not None:
        sets.append("valid_stand=excluded.valid_stand")
    if intensity_min is not None:
        # intensity 列建表就有但一直写 0；这里复用它存中高强度活动分钟
        sets.append("intensity=excluded.intensity")
    if sleep_score is not None:
        sets.append("sleep_score=excluded.sleep_score")
    if sleep_awake is not None:
        sets.append("sleep_awake=excluded.sleep_awake")
    if awake_count is not None:
        sets.append("awake_count=excluded.awake_count")
    if sleep_start is not None:
        sets.append("sleep_start=excluded.sleep_start")
    if sleep_end is not None:
        sets.append("sleep_end=excluded.sleep_end")
    if sleep_avg_hr is not None:
        sets.append("sleep_avg_hr=excluded.sleep_avg_hr")
    if sleep_max_hr is not None:
        sets.append("sleep_max_hr=excluded.sleep_max_hr")
    await db.execute(
        f"""
        INSERT INTO health_miband_activity (
            source, measured_at, device_name, raw_kind, intensity, steps,
            heart_rate, unknown_value, sleep_value, deep_sleep_value,
            rem_sleep_value, sleep_stage, calories, valid_stand,
            sleep_score, sleep_awake, awake_count, sleep_start, sleep_end,
            sleep_avg_hr, sleep_max_hr, synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source, measured_at) DO UPDATE SET {', '.join(sets)}
        """,
        (
            source, measured_at, device_name, 0, _val(intensity_min), _val(steps),
            _val(heart_rate), 0, _val(sleep), _val(deep_sleep), _val(rem_sleep),
            sleep_stage or "", _val(calories), _val(valid_stand),
            _val(sleep_score), _val(sleep_awake), _val(awake_count),
            float(sleep_start) if sleep_start is not None else 0,
            float(sleep_end) if sleep_end is not None else 0,
            _val(sleep_avg_hr), _val(sleep_max_hr), now,
        ),
    )


async def _sync_day(
    client,
    uid: int,
    day: date,
    device_name: str,
    now: float,
) -> dict[str, Any]:
    """拉指定一天的云端日汇总入库。返回该天各类型写入条数与错误。

    先把三类 API 结果拉到内存，再开**一个** DB 连接批量 upsert + 单次 commit，
    任一写入失败回滚整天，避免心率已更新但睡眠没写成的部分可见状态。
    """
    counts: dict[str, Any] = {"heart": 0, "steps": 0, "sleep": 0}
    day_start = _day_start_ts(day)
    pending: list[dict[str, Any]] = []

    # 心率（日均，SDK time 是秒级 Unix 时间戳，不是毫秒）
    try:
        hrs = await client.get_heart_rate(uid, day, days=1)
        for hr in hrs:
            if hr.avg_hr and 20 <= hr.avg_hr <= 240:
                pending.append({"heart_rate": hr.avg_hr})
                counts["heart"] += 1
    except Exception as e:
        counts["heart_err"] = str(e)

    # 步数（当日总，SDK time 是秒级 Unix 时间戳，不是毫秒）
    try:
        steps_list = await client.get_steps(uid, day, days=1)
        for st in steps_list:
            if st.steps > 0:
                pending.append({"steps": st.steps})
                counts["steps"] += 1
    except Exception as e:
        counts["steps_err"] = str(e)

    # 睡眠（一天一条汇总：总时长 + 深睡/浅睡/REM + 评分/清醒/入睡醒来时间/夜间心率）。
    # 云端只有日粒度汇总，segment_details 给入睡/醒来时间戳但不是逐分钟时间线。
    try:
        sleeps = await client.get_sleep(uid, day, days=1)
        for sl in sleeps:
            deep = max(0, int(sl.sleep_deep_duration))
            light = max(0, int(sl.sleep_light_duration))
            rem = max(0, int(sl.sleep_rem_duration))
            total = int(sl.total_duration) or (deep + light + rem)
            if total <= 0:
                continue
            # 入睡/醒来时间：取第一个 segment 的 bedtime/wake_up_time（秒级时间戳）
            sleep_start = 0.0
            sleep_end = 0.0
            awake_cnt = 0
            segs = sl.segment_details or []
            if segs:
                first = segs[0]
                sleep_start = float(first.bedtime or 0)
                sleep_end = float(first.wake_up_time or 0)
                awake_cnt = int(first.awake_count or 0)
            pending.append({
                "sleep": total,
                "deep_sleep": deep,
                "rem_sleep": rem,
                "sleep_score": int(sl.sleep_score or 0),
                "sleep_awake": int(sl.sleep_awake_duration or 0),
                "awake_count": awake_cnt,
                "sleep_start": sleep_start,
                "sleep_end": sleep_end,
                "sleep_avg_hr": int(sl.avg_hr or 0),
                "sleep_max_hr": int(sl.max_hr or 0),
            })
            counts["sleep"] += 1
    except Exception as e:
        counts["sleep_err"] = str(e)

    if pending:
        async with get_db() as db:
            for item in pending:
                await _persist_sample(
                    db, measured_at=day_start, device_name=device_name,
                    source=SOURCE,
                    heart_rate=item.get("heart_rate"),
                    steps=item.get("steps"),
                    sleep_stage=item.get("sleep_stage"),
                    sleep=item.get("sleep"),
                    deep_sleep=item.get("deep_sleep"),
                    rem_sleep=item.get("rem_sleep"),
                    sleep_score=item.get("sleep_score"),
                    sleep_awake=item.get("sleep_awake"),
                    awake_count=item.get("awake_count"),
                    sleep_start=item.get("sleep_start"),
                    sleep_end=item.get("sleep_end"),
                    sleep_avg_hr=item.get("sleep_avg_hr"),
                    sleep_max_hr=item.get("sleep_max_hr"),
                    now=now,
                )
            await db.commit()

    return counts


async def _sync_latest_snapshot(
    client,
    uid: int,
    device_name: str,
    now: float,
) -> dict[str, Any]:
    """拉亲友最新快照全量 → 落库心率/血氧/血压/体重/目标/卡路里/活动强度/站立。

    原实现只取 snapshot.heart_rate 一个字段，把旁边 9 类全扔了——授权了却拿不到的
    真因。这里一次把 LatestDataSnapshot 全字段按各自落点写入，单连接单 commit。

    落点：
      - 心率 → health_ring_heart_rates（真实采样时间，与日均值区分）
      - 血氧/血压/目标 → health_ring_latest(id=1) 局部更新，不覆盖戒指写的睡眠等列
      - 体重 → health_weight_entries（按日期去重，前端日历可见）
      - 卡路里/活动强度/站立 → health_miband_activity 今天日汇总行（当日聚合值）
    血压 value 为 {} 时（手环没手动测过）跳过不写，不拿默认 0 冒充。
    """
    try:
        snapshot = await client.get_latest_data(uid)
    except Exception as e:
        return {"error": str(e)}
    if not snapshot:
        return {"written": 0}

    result: dict[str, Any] = {}
    updated_ts = float(snapshot.updated_time or now)
    hr_item = snapshot.heart_rate

    async with get_db() as db:
        # ── a) 心率 → health_ring_heart_rates ──
        hr_entry = None
        hr_written = 0
        hr_events: list[Any] = []
        if hr_item and hr_item.bpm and (20 <= int(hr_item.bpm) <= 240):
            hr_ts = float(hr_item.time or updated_ts or now)
            raw = {
                "kind": "latest_snapshot",
                "account_updated_at": hr_ts,
                "device_name": device_name,
            }
            hr_entry = await insert_heart_rate(
                db, device_name=device_name, heart_rate=int(hr_item.bpm),
                measured_at=hr_ts, source=SOURCE, raw=raw,
            )
            hr_written = 1 if hr_entry else 0
            hr_events = (
                await analyze_heart_rate_entry(db, hr_entry)
                if hr_entry and hr_entry.get("is_new") else []
            )
        result["heart_rate"] = int(hr_item.bpm) if (hr_item and hr_item.bpm and hr_entry) else 0
        result["events"] = len(hr_events or [])

        # ── b) 血氧/血压/目标 → health_ring_latest(id=1) 局部更新 ──
        # 只动这几个列，不碰戒指写的 sleep/heart_rate 等。
        # 先确保 id=1 行存在（INSERT OR IGNORE 空壳），再逐字段 UPDATE。
        ring_raw: dict[str, Any] = {"kind": "latest_snapshot", "source": SOURCE}
        await db.execute(
            "INSERT OR IGNORE INTO health_ring_latest (id, device_name, synced_at) VALUES (1, ?, ?)",
            (device_name, now),
        )
        ring_updates: list[tuple[str, Any]] = []
        spo2_item = snapshot.spo2
        if spo2_item and spo2_item.spo2 and (50 <= int(spo2_item.spo2) <= 100):
            ring_updates.append(("spo2", int(spo2_item.spo2)))
            ring_raw["spo2"] = {"value": int(spo2_item.spo2), "time": spo2_item.time}
            result["spo2"] = int(spo2_item.spo2)

        # 血压：value={} 时模型解析成 systolic=0/diastolic=0，靠 systolic>0 判真数据
        bp_item = snapshot.blood_pressure
        if bp_item and bp_item.systolic > 0 and bp_item.diastolic > 0:
            ring_updates.append(("systolic_bp", int(bp_item.systolic)))
            ring_updates.append(("diastolic_bp", int(bp_item.diastolic)))
            ring_raw["blood_pressure"] = {
                "systolic": int(bp_item.systolic),
                "diastolic": int(bp_item.diastolic),
                "time": bp_item.time,
            }
            result["blood_pressure"] = f"{int(bp_item.systolic)}/{int(bp_item.diastolic)}"

        # 目标完成度 → goal_raw JSON
        goal_item = snapshot.goal
        if goal_item and goal_item.goal_items:
            goal_json = json.dumps(
                [
                    {"field": it.field, "metric": it.metric_key,
                     "label": it.metric_label, "target": it.target_value,
                     "achieved": it.achieved_value}
                    for it in goal_item.goal_items
                ],
                ensure_ascii=False,
            )
            ring_updates.append(("goal_raw", goal_json))
            ring_raw["goal"] = goal_json
            result["goal_items"] = len(goal_item.goal_items)

        if ring_updates:
            set_clause = ", ".join(f"{col}=?" for col, _ in ring_updates)
            set_vals = [v for _, v in ring_updates]
            await db.execute(
                f"UPDATE health_ring_latest SET {set_clause}, "
                f"raw_json=?, synced_at=? WHERE id=1",
                (*set_vals, json.dumps(ring_raw, ensure_ascii=False), now),
            )

        # ── c) 卡路里/活动强度/站立 → health_miband_activity 今天日汇总行 ──
        today = date.today()
        day_start = _day_start_ts(today)
        cal_val = None
        stand_val = None
        intensity_val = None
        cal_item = snapshot.calories
        if cal_item and cal_item.calories >= 0:
            cal_val = int(cal_item.calories)
            result["calories"] = cal_val
        stand_item = snapshot.valid_stand
        if stand_item and stand_item.count >= 0:
            stand_val = int(stand_item.count)
            result["valid_stand"] = stand_val
        inten_item = snapshot.intensity
        if inten_item and inten_item.duration >= 0:
            intensity_val = int(inten_item.duration)
            result["intensity"] = intensity_val
        if any(v is not None for v in (cal_val, stand_val, intensity_val)):
            await _persist_sample(
                db, measured_at=day_start, device_name=device_name,
                source=SOURCE,
                calories=cal_val, valid_stand=stand_val, intensity_min=intensity_val,
                now=now,
            )

        # ── d) 体重 → health_weight_entries（按日期去重，前端日历可见）──
        weight_item = snapshot.weight
        if weight_item and weight_item.weight and 20 <= float(weight_item.weight) <= 300:
            w_ts = float(weight_item.time or updated_ts or now)
            w_date = datetime.fromtimestamp(w_ts).date().isoformat()
            await db.execute(
                """
                INSERT INTO health_weight_entries (date, weight_kg, note, created_at, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                    weight_kg=excluded.weight_kg,
                    updated_at=excluded.updated_at
                """,
                (w_date, float(weight_item.weight), "☁ 小米云同步", now, now),
            )
            result["weight"] = {"date": w_date, "kg": float(weight_item.weight)}

        await db.commit()

    result["written"] = hr_written
    return result


async def _do_sync_once() -> dict[str, Any]:
    """同步云端数据：首次补最近 7 天，之后每次只拉今天 + 失败天。

    同步状态持久化在 data/mi_cloud_sync_state.json（backfilled_through +
    failed_days），重启不重拉整段历史，只重试失败天。只有当轮全部同步天
    无任何 *_err 时才推进 backfilled_through、执行 45 天清理；否则保留
    failed_days 下轮重试，且不清理（避免删了还没补齐的旧数据）。
    """
    if not TOKEN_PATH.exists():
        return {"error": 0, "reason": "token 不存在，先跑 mi_cloud_login.py"}

    from mi_fitness import MiHealthClient

    today = date.today()
    now = time.time()
    device_name = _load_settings().get("mi_cloud", {}).get("device_name", DEVICE_DEFAULT)
    counts: dict[str, Any] = {"heart": 0, "steps": 0, "sleep": 0}

    state = _load_sync_state()
    backfilled_through = state.get("backfilled_through")
    prev_failed: set[str] = set(state.get("failed_days") or [])

    try:
        bd = date.fromisoformat(backfilled_through) if backfilled_through else None
    except ValueError:
        bd = None
    if bd is None:
        # 首次：补最近 HISTORY_BACKFILL_DAYS 天（含今天）
        bd = today - timedelta(days=HISTORY_BACKFILL_DAYS - 1)

    # 本轮要同步的天：今天 + (backfilled_through, today) 开区间 + 历史失败天
    days: list[date] = [today]
    d = today - timedelta(days=1)
    while d > bd:
        days.append(d)
        d -= timedelta(days=1)
    for iso in prev_failed:
        try:
            fd = date.fromisoformat(iso)
        except ValueError:
            continue
        if fd != today and fd not in days:
            days.append(fd)

    run_failed: set[str] = set()

    async with MiHealthClient.from_token(str(TOKEN_PATH)) as client:
        uid, reason = await _fetch_relative_uid(client)
        if not uid:
            counts["reason"] = reason or "未配置 relative_uid"
            return counts
        for day in days:
            day_counts = await _sync_day(client, uid, day, device_name, now)
            day_had_error = False
            for key in ("heart", "steps", "sleep"):
                counts[key] += int(day_counts.get(key, 0))
            for err_key in ("heart_err", "steps_err", "sleep_err"):
                if day_counts.get(err_key):
                    counts[err_key] = day_counts[err_key]
                    day_had_error = True
            if day_had_error:
                run_failed.add(_day_iso(day))
            else:
                prev_failed.discard(_day_iso(day))
        # 每轮都拉一次最新快照心率（真实采样时间）
        counts["snapshot"] = await _sync_latest_snapshot(client, uid, device_name, now)

    has_day_errors = bool(run_failed)

    # 推进 backfilled_through：只有今天之前的天全部成功才推进到昨天。
    # today 的数据云端可能继续更新，不纳入 backfilled_through。
    if not has_day_errors:
        state["backfilled_through"] = (today - timedelta(days=1)).isoformat()
    state["failed_days"] = sorted(run_failed | prev_failed)
    state["last_run_at"] = now
    _save_sync_state(state)

    # 45 天清理绑定成功：本轮任何 *_err 都跳过，避免删了没补齐的旧数据。
    # 快照心率在 health_ring_heart_rates 里有按源保留策略，不在这里删。
    if not has_day_errors:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM health_miband_activity "
                "WHERE source=? AND measured_at < ?",
                (SOURCE, now - 45 * 86400),
            )
            await db.commit()

    return counts


async def _sync_once() -> dict[str, Any]:
    """统一同步入口：进程内加锁，避免后台轮询与手动 API 并发写库。"""
    async with _sync_lock:
        return await _do_sync_once()


async def mi_cloud_health_loop():
    """后台轮询：每 POLL_INTERVAL 秒拉一次当天数据入库。"""
    print("[mi_cloud_health] 轮询任务启动")
    while True:
        try:
            result = await _sync_once()
            if result.get("reason"):
                print(f"[mi_cloud_health] 跳过：{result['reason']}")
            else:
                print(f"[mi_cloud_health] 同步完成：{result}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[mi_cloud_health] ❌ 异常：{e}")
        await asyncio.sleep(POLL_INTERVAL)


async def sync_mi_cloud_now() -> dict[str, Any]:
    """手动触发一次同步（供 API 调用）。"""
    return await _sync_once()
