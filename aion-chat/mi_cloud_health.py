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
from typing import Any

import aiosqlite

from database import get_db
from health_context import analyze_heart_rate_entry, insert_heart_rate

DATA_DIR = Path(__file__).parent / "data"
TOKEN_PATH = DATA_DIR / "mi_cloud_token.json"
SOURCE = "mi_cloud"
DEVICE_DEFAULT = "Redmi Smart Band 2"

# 轮询间隔（秒）。云端数据本身有延迟，5 分钟足够。
POLL_INTERVAL = 5 * 60

# 首次启动时补拉的历史天数：小米云端睡眠/步数隔天才有完整汇总，
# 不补的话昨天和更早的有效数据永远进不来。
HISTORY_BACKFILL_DAYS = 7

# 进程内标记：历史回填只做一次，之后每次轮询只拉当天。
_backfilled = False

# 进程内同步锁：后台轮询与手动 /api/health/mi-band/cloud-sync 不能同时写库。
_sync_lock = asyncio.Lock()


def _load_settings() -> dict[str, Any]:
    try:
        with open(DATA_DIR / "settings.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _day_start_ts(day: date) -> float:
    """当天本地 0 点的时间戳。日汇总行统一用它当 measured_at，三次接口可合并到一行。"""
    return datetime(day.year, day.month, day.day).timestamp()


async def _fetch_relative_uid(client) -> int | None:
    """从 settings 读 relative_uid；没有则取亲友列表第一个。"""
    cfg = _load_settings().get("mi_cloud", {})
    uid = cfg.get("relative_uid")
    if uid:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    relatives = await client.get_relatives()
    if not relatives:
        return None
    return relatives[0].relative_uid


async def _persist_sample(
    db: aiosqlite.Connection,
    *,
    measured_at: float,
    device_name: str,
    source: str = SOURCE,
    heart_rate: int,
    steps: int,
    sleep_stage: str,
    sleep: int,
    deep_sleep: int,
    rem_sleep: int,
    now: float,
) -> None:
    # ON CONFLICT 时只覆盖本次非零字段：同一天一行里，心率/步数/睡眠可能由
    # 三次独立接口分别写入，后写的不应把先写的字段清成 0。
    # heart_rate=0 / steps=0 / sleep=0 这些“空值”用 COALESCE(NULLIF(excluded,...), old) 保留旧值。
    await db.execute(
        """
        INSERT INTO health_miband_activity (
            source, measured_at, device_name, raw_kind, intensity, steps,
            heart_rate, unknown_value, sleep_value, deep_sleep_value,
            rem_sleep_value, sleep_stage, synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source, measured_at) DO UPDATE SET
            device_name=excluded.device_name,
            steps=COALESCE(NULLIF(excluded.steps,0), health_miband_activity.steps),
            heart_rate=COALESCE(NULLIF(excluded.heart_rate,0), health_miband_activity.heart_rate),
            sleep_value=COALESCE(NULLIF(excluded.sleep_value,0), health_miband_activity.sleep_value),
            deep_sleep_value=COALESCE(NULLIF(excluded.deep_sleep_value,0), health_miband_activity.deep_sleep_value),
            rem_sleep_value=COALESCE(NULLIF(excluded.rem_sleep_value,0), health_miband_activity.rem_sleep_value),
            sleep_stage=CASE WHEN excluded.sleep_stage <> '' THEN excluded.sleep_stage
                             ELSE health_miband_activity.sleep_stage END,
            synced_at=excluded.synced_at
        """,
        (
            source, measured_at, device_name, 0, 0, max(0, steps),
            heart_rate, 0, sleep, deep_sleep, rem_sleep, sleep_stage, now,
        ),
    )


async def _sync_day(
    client,
    uid: int,
    day: date,
    device_name: str,
    now: float,
) -> dict[str, int]:
    """拉指定一天的云端日汇总入库。返回该天各类型写入条数。"""
    counts = {"heart": 0, "steps": 0, "sleep": 0}
    day_start = _day_start_ts(day)

    # 心率（日均，SDK time 是秒级 Unix 时间戳，不是毫秒）
    try:
        hrs = await client.get_heart_rate(uid, day, days=1)
        for hr in hrs:
            if hr.avg_hr and 20 <= hr.avg_hr <= 240:
                async with get_db() as db:
                    await _persist_sample(
                        db, measured_at=day_start, device_name=device_name,
                        source=SOURCE, heart_rate=hr.avg_hr, steps=0, sleep_stage="",
                        sleep=0, deep_sleep=0, rem_sleep=0, now=now,
                    )
                    await db.commit()
                counts["heart"] += 1
    except Exception as e:
        counts["heart_err"] = str(e)

    # 步数（当日总，SDK time 是秒级 Unix 时间戳，不是毫秒）
    try:
        steps_list = await client.get_steps(uid, day, days=1)
        for st in steps_list:
            if st.steps > 0:
                async with get_db() as db:
                    await _persist_sample(
                        db, measured_at=day_start, device_name=device_name,
                        source=SOURCE, heart_rate=0, steps=st.steps, sleep_stage="",
                        sleep=0, deep_sleep=0, rem_sleep=0, now=now,
                    )
                    await db.commit()
                counts["steps"] += 1
    except Exception as e:
        counts["steps_err"] = str(e)

    # 睡眠（一天一条汇总：总时长 + 深睡/浅睡/REM 各分钟数）。
    # 云端只有日粒度汇总，没有逐分钟时间线，不展开、不伪造 stage 行；
    # 总时长存 sleep_value，深/浅/REM 存对应列，前端按 precision=daily 展示。
    try:
        sleeps = await client.get_sleep(uid, day, days=1)
        for sl in sleeps:
            deep = max(0, int(sl.sleep_deep_duration))
            light = max(0, int(sl.sleep_light_duration))
            rem = max(0, int(sl.sleep_rem_duration))
            total = deep + light + rem
            if total <= 0:
                continue
            async with get_db() as db:
                await _persist_sample(
                    db, measured_at=day_start, device_name=device_name,
                    source=SOURCE, heart_rate=0, steps=0, sleep_stage="",
                    sleep=total, deep_sleep=deep, rem_sleep=rem, now=now,
                )
                await db.commit()
            counts["sleep"] += 1
    except Exception as e:
        counts["sleep_err"] = str(e)

    return counts


async def _sync_latest_snapshot(
    client,
    uid: int,
    device_name: str,
    now: float,
) -> dict[str, Any]:
    """拉亲友最新实时快照心率 → health_ring_heart_rates（source=mi_cloud）。

    云端聚合接口有延迟，最新心率以 get_latest_data 的真实采样时间为准，
    不与日均值混为一谈；raw_json 记录 kind 与账号数据时间，方便溯源。
    """
    try:
        snapshot = await client.get_latest_data(uid)
    except Exception as e:
        return {"error": str(e)}
    if not snapshot:
        return {"written": 0}
    hr_item = snapshot.heart_rate
    if not hr_item or not hr_item.bpm or not (20 <= int(hr_item.bpm) <= 240):
        return {"written": 0}
    ts = float(hr_item.time or snapshot.updated_time or now)
    raw = {
        "kind": "latest_snapshot",
        "account_updated_at": ts,
        "device_name": device_name,
    }
    async with get_db() as db:
        entry = await insert_heart_rate(
            db, device_name=device_name, heart_rate=int(hr_item.bpm),
            measured_at=ts, source=SOURCE, raw=raw,
        )
        events = (
            await analyze_heart_rate_entry(db, entry)
            if entry and entry.get("is_new") else []
        )
        await db.commit()
    return {
        "written": 1 if entry else 0,
        "heart_rate": int(hr_item.bpm) if entry else 0,
        "events": len(events or []),
    }


async def _do_sync_once() -> dict[str, Any]:
    """同步云端数据：首次调用补最近 7 天，之后每次只拉当天。"""
    global _backfilled
    if not TOKEN_PATH.exists():
        return {"error": 0, "reason": "token 不存在，先跑 mi_cloud_login.py"}

    from mi_fitness import MiHealthClient

    today = date.today()
    now = time.time()
    device_name = _load_settings().get("mi_cloud", {}).get("device_name", DEVICE_DEFAULT)
    counts = {"heart": 0, "steps": 0, "sleep": 0}

    # 首启补历史：day 从今天往前逐天，先今天、再昨天…避免中途失败时缺口在最新数据。
    days = [today]
    if not _backfilled:
        days += [today - timedelta(days=n) for n in range(1, HISTORY_BACKFILL_DAYS)]

    async with MiHealthClient.from_token(str(TOKEN_PATH)) as client:
        uid = await _fetch_relative_uid(client)
        if not uid:
            counts["reason"] = "亲友列表为空或未配置 relative_uid"
            return counts
        for day in days:
            day_counts = await _sync_day(client, uid, day, device_name, now)
            for key in ("heart", "steps", "sleep"):
                counts[key] += int(day_counts.get(key, 0))
            for err_key in ("heart_err", "steps_err", "sleep_err"):
                if day_counts.get(err_key):
                    counts[err_key] = day_counts[err_key]
        # 每轮都拉一次最新快照心率（真实采样时间）
        counts["snapshot"] = await _sync_latest_snapshot(client, uid, device_name, now)

    # 清理 45 天前的云日汇总（与 BLE 路径 TTL 对齐），避免只增不删。
    # 快照心率在 health_ring_heart_rates 里有按源保留策略，不在这里删。
    async with get_db() as db:
        await db.execute(
            "DELETE FROM health_miband_activity "
            "WHERE source=? AND measured_at < ?",
            (SOURCE, now - 45 * 86400),
        )
        await db.commit()

    # 只有整轮成功才标记回填完成；中途异常会抛出，_backfilled 保持 False，下轮补拉。
    _backfilled = True
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
