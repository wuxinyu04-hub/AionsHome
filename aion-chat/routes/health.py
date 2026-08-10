"""
健康功能 API：戒指最新快照、体重记录、姨妈期记录。
"""

import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel

from database import get_db
from health_context import (
    analyze_heart_rate_entry,
    get_heart_config,
    get_heart_events,
    insert_heart_rate,
    update_heart_config,
)
from ws import manager

router = APIRouter(prefix="/api/health", tags=["health"])

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ring_diag_info = {"info": "", "status": "", "ts": 0}


def _valid_date(value: str) -> bool:
    if not value or not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _row_dict(row):
    return dict(row) if row else None


class RingSleep(BaseModel):
    start_at: Optional[float] = None
    end_at: Optional[float] = None
    total_min: Optional[int] = None
    deep_min: Optional[int] = None
    light_min: Optional[int] = None
    rem_min: Optional[int] = None
    wake_min: Optional[int] = None
    wake_count: Optional[int] = None


class RingSnapshot(BaseModel):
    device_name: str = ""
    heart_rate: Optional[int] = None
    systolic_bp: Optional[int] = None
    diastolic_bp: Optional[int] = None
    spo2: Optional[int] = None
    hrv: Optional[float] = None
    measured_at: Optional[float] = None
    sleep: Optional[RingSleep] = None
    raw: Optional[dict] = None


class HeartRateSample(BaseModel):
    device_name: str = ""
    heart_rate: int
    measured_at: Optional[float] = None
    source: str = ""
    raw: Optional[dict] = None


class RingHeartRate(HeartRateSample):
    """Legacy request name kept for the existing ring endpoint."""


class MiBandActivitySample(BaseModel):
    measured_at: float
    raw_kind: int = 0
    intensity: int = 0
    steps: int = 0
    heart_rate: int = 0
    unknown: int = 0
    sleep: int = 0
    deep_sleep: int = 0
    rem_sleep: int = 0
    sleep_stage: str = ""


class MiBandActivityBatch(BaseModel):
    device_name: str = "Xiaomi Smart Band 7"
    samples: List[MiBandActivitySample]


class HeartConfigUpdate(BaseModel):
    sleep_low_max: Optional[int] = None
    normal_min: Optional[int] = None
    normal_max: Optional[int] = None
    elevated_min: Optional[int] = None
    exercise_min: Optional[int] = None
    attention_low: Optional[int] = None
    attention_high: Optional[int] = None
    large_delta: Optional[int] = None
    night_start_hour: Optional[int] = None
    night_end_hour: Optional[int] = None
    stale_minutes: Optional[int] = None


class RingDiagReport(BaseModel):
    status: str = ""
    info: str = ""


class WeightEntry(BaseModel):
    date: str
    weight_kg: float
    note: str = ""


class PeriodEntry(BaseModel):
    id: str = ""
    start_date: str
    end_date: str = ""
    flow: str = ""
    symptoms: str = ""
    note: str = ""


def _valid_heart_rate(value: Optional[int]) -> bool:
    return isinstance(value, int) and 20 <= value <= 240


async def _insert_heart_rate(
    db,
    *,
    device_name: str,
    heart_rate: Optional[int],
    measured_at: Optional[float],
    source: str,
    raw: Optional[dict] = None,
):
    # 统一走 health_context 的可复用写入（同一去重/清理逻辑）
    return await insert_heart_rate(
        db,
        device_name=device_name,
        heart_rate=heart_rate,
        measured_at=measured_at,
        source=source,
        raw=raw,
    )


async def _recent_heart_rates(db, limit: int = 20):
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, device_name, heart_rate, measured_at, source, raw_json, created_at "
        "FROM health_ring_heart_rates ORDER BY measured_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _recent_mi_band_heart_rates(db, limit: int = 20):
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, device_name, heart_rate, measured_at, source, raw_json, created_at "
        "FROM health_ring_heart_rates WHERE source='mi_band_7' "
        "ORDER BY measured_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _recent_cloud_heart_rates(db, limit: int = 20):
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, device_name, heart_rate, measured_at, source, raw_json, created_at "
        "FROM health_ring_heart_rates WHERE source='mi_cloud' "
        "ORDER BY measured_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


def _load_health_settings() -> dict:
    try:
        with open(
            Path(__file__).resolve().parent.parent / "data" / "settings.json",
            encoding="utf-8",
        ) as f:
            return json.load(f)
    except Exception:
        return {}


async def _cloud_mode_active(db) -> bool:
    """云模式激活：settings 配了 mi_cloud，或已有 mi_cloud 数据落库。"""
    if _load_health_settings().get("mi_cloud", {}).get("relative_uid"):
        return True
    cur = await db.execute(
        "SELECT 1 FROM health_miband_activity WHERE source='mi_cloud' LIMIT 1"
    )
    if await cur.fetchone():
        return True
    cur = await db.execute(
        "SELECT 1 FROM health_ring_heart_rates WHERE source='mi_cloud' LIMIT 1"
    )
    return await cur.fetchone() is not None


async def _build_cloud_summary(db, current: float) -> dict:
    """云端日汇总模式：日均心率/每日步数/睡眠总时长 + 最新快照心率。"""
    settings = _load_health_settings().get("mi_cloud", {})
    device_name = settings.get("device_name") or "Redmi Smart Band 2"
    # 最新快照心率（真实采样时间，写入 health_ring_heart_rates）
    latest = await (await db.execute(
        "SELECT id, device_name, heart_rate, measured_at, source, raw_json, created_at "
        "FROM health_ring_heart_rates WHERE source='mi_cloud' "
        "ORDER BY measured_at DESC LIMIT 1"
    )).fetchone()
    # 最近一条云日汇总（日均心率/步数/睡眠总时长，measured_at=当天 0 点）
    latest_day = await (await db.execute(
        "SELECT device_name, measured_at, heart_rate, steps, sleep_value, "
        "deep_sleep_value, rem_sleep_value, calories, valid_stand, intensity, "
        "sleep_score, sleep_awake, awake_count, sleep_start, sleep_end, "
        "sleep_avg_hr, sleep_max_hr, synced_at "
        "FROM health_miband_activity WHERE source='mi_cloud' "
        "ORDER BY measured_at DESC LIMIT 1"
    )).fetchone()
    # health_ring_latest id=1：mi_cloud 快照写的血氧/血压/目标（云模式用户没连戒指，
    # 这一行由 mi_cloud 独占写入）。读出来镜像给前端，避免云模式卡片空白。
    ring_snap = await (await db.execute(
        "SELECT spo2, systolic_bp, diastolic_bp, goal_raw, synced_at "
        "FROM health_ring_latest WHERE id=1"
    )).fetchone()
    # 最新体重（health_weight_entries 按 date 去重，前端日历也读这表）
    weight_row = await (await db.execute(
        "SELECT date, weight_kg FROM health_weight_entries ORDER BY date DESC LIMIT 1"
    )).fetchone()
    local_now = datetime.fromtimestamp(current)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = await (await db.execute(
        "SELECT COALESCE(SUM(steps),0) AS steps FROM health_miband_activity "
        "WHERE source='mi_cloud' AND measured_at>=? AND measured_at<?",
        (day_start, day_start + 86400),
    )).fetchone()
    # 最近一条有睡眠的云日汇总（含入睡/醒来时间/评分/清醒/夜间心率，无逐分钟时间线）
    sleep_day = await (await db.execute(
        "SELECT measured_at, sleep_value, deep_sleep_value, rem_sleep_value, "
        "sleep_score, sleep_awake, awake_count, sleep_start, sleep_end, "
        "sleep_avg_hr, sleep_max_hr "
        "FROM health_miband_activity WHERE source='mi_cloud' "
        "AND (sleep_value>0 OR deep_sleep_value>0 OR rem_sleep_value>0) "
        "ORDER BY measured_at DESC LIMIT 1"
    )).fetchone()
    sleep_summary = None
    if sleep_day:
        total = int(sleep_day["sleep_value"] or 0)
        deep = int(sleep_day["deep_sleep_value"] or 0)
        rem = int(sleep_day["rem_sleep_value"] or 0)
        light = max(0, total - deep - rem) if total > 0 else 0
        # 入睡/醒来时间戳（秒级，来自 segment_details）
        sleep_start = float(sleep_day["sleep_start"] or 0)
        sleep_end = float(sleep_day["sleep_end"] or 0)
        sleep_summary = {
            "precision": "daily",
            "sleepDate": datetime.fromtimestamp(
                float(sleep_day["measured_at"])
            ).date().isoformat(),
            "startAt": sleep_start if sleep_start > 0 else None,
            "endAt": sleep_end if sleep_end > 0 else None,
            "kind": "main" if total >= 180 else ("nap" if total > 0 else None),
            "totalMin": total if total > 0 else None,
            "deepMin": deep if deep > 0 else None,
            "lightMin": light if light > 0 else None,
            "remMin": rem if rem > 0 else None,
            # 睡眠扩展字段
            "score": int(sleep_day["sleep_score"] or 0) or None,
            "awakeMin": int(sleep_day["sleep_awake"] or 0) or None,
            "awakeCount": int(sleep_day["awake_count"] or 0) or None,
            "avgHr": int(sleep_day["sleep_avg_hr"] or 0) or None,
            "maxHr": int(sleep_day["sleep_max_hr"] or 0) or None,
            "sessions": [],
        }
    return {
        "mode": "mi_cloud",
        "sourceLabel": f"{device_name} · 小米云",
        "deviceName": device_name,
        "lastSyncAt": latest_day["synced_at"] if latest_day else 0,
        "cloudUpdatedAt": latest_day["measured_at"] if latest_day else 0,
        "latestHeartRate": int(latest["heart_rate"]) if latest else None,
        "latestHeartRateAt": latest["measured_at"] if latest else 0,
        "heartRateKind": "latest_snapshot",
        "dailyAverageHeartRate": (
            int(latest_day["heart_rate"]) if latest_day and latest_day["heart_rate"] else None
        ),
        "dailyAverageHeartRateAt": latest_day["measured_at"] if latest_day else 0,
        "todaySteps": int(today["steps"] if today else 0),
        "stepsKind": "daily_total",
        "todayCalories": (
            int(latest_day["calories"]) if latest_day and latest_day["calories"] else 0
        ),
        "todayValidStand": (
            int(latest_day["valid_stand"]) if latest_day and latest_day["valid_stand"] else 0
        ),
        "todayIntensity": (
            int(latest_day["intensity"]) if latest_day and latest_day["intensity"] else 0
        ),
        # 血氧/血压/目标：mi_cloud 快照写进 health_ring_latest(id=1)，云模式镜像一份
        "spo2": int(ring_snap["spo2"]) if ring_snap and ring_snap["spo2"] else None,
        "bloodPressure": (
            f"{int(ring_snap['systolic_bp'])}/{int(ring_snap['diastolic_bp'])}"
            if ring_snap and ring_snap["systolic_bp"] and ring_snap["systolic_bp"] > 0
            else None
        ),
        "goal": ring_snap["goal_raw"] if ring_snap and ring_snap["goal_raw"] else None,
        # 体重：health_weight_entries 最新一条
        "weight": (
            {"date": weight_row["date"], "kg": float(weight_row["weight_kg"])}
            if weight_row else None
        ),
        "activityMinutes": None,
        "recent30ActivityMinutes": None,
        "recent30Steps": None,
        "recent60ActivityMinutes": None,
        "recent60Steps": None,
        "activityDataThrough": 0,
        "supportsRecentActivity": False,
        "sleep": sleep_summary,
        "recentHeartRates": await _recent_cloud_heart_rates(db, 20),
    }


async def build_mi_band_summary(db, now: Optional[float] = None):
    current = time.time() if now is None else float(now)
    db.row_factory = aiosqlite.Row
    # 云模式（红米手环 2 · 小米云）优先；否则走原有 Mi Band 7 BLE 分钟采样
    if await _cloud_mode_active(db):
        return await _build_cloud_summary(db, current)
    local_now = datetime.fromtimestamp(current)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    latest = await (await db.execute(
        "SELECT device_name, measured_at, synced_at FROM health_miband_activity "
        "WHERE source='mi_band_7' ORDER BY measured_at DESC LIMIT 1"
    )).fetchone()
    totals = await (await db.execute(
        "SELECT COALESCE(SUM(steps),0) AS steps, "
        "COALESCE(SUM(CASE WHEN steps > 0 THEN 1 ELSE 0 END),0) AS active_minutes "
        "FROM health_miband_activity WHERE source='mi_band_7' AND measured_at>=? AND measured_at<?",
        (day_start, day_start + 86400),
    )).fetchone()
    recent_activity = {
        "activityDataThrough": latest["measured_at"] if latest else 0,
        "recent30ActivityMinutes": 0,
        "recent30Steps": 0,
        "recent60ActivityMinutes": 0,
        "recent60Steps": 0,
    }
    if latest:
        latest_at = float(latest["measured_at"])
        recent = await (await db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN measured_at>? AND steps>0 THEN 1 ELSE 0 END),0) AS active_30, "
            "COALESCE(SUM(CASE WHEN measured_at>? THEN steps ELSE 0 END),0) AS steps_30, "
            "COALESCE(SUM(CASE WHEN steps>0 THEN 1 ELSE 0 END),0) AS active_60, "
            "COALESCE(SUM(steps),0) AS steps_60 "
            "FROM health_miband_activity WHERE source='mi_band_7' "
            "AND measured_at>? AND measured_at<=?",
            (latest_at - 30 * 60, latest_at - 30 * 60, latest_at - 60 * 60, latest_at),
        )).fetchone()
        recent_activity.update({
            "recent30ActivityMinutes": int(recent["active_30"]),
            "recent30Steps": int(recent["steps_30"]),
            "recent60ActivityMinutes": int(recent["active_60"]),
            "recent60Steps": int(recent["steps_60"]),
        })
    latest_heart = await (await db.execute(
        "SELECT heart_rate, measured_at FROM health_miband_activity "
        "WHERE source='mi_band_7' AND heart_rate BETWEEN 20 AND 240 "
        "ORDER BY measured_at DESC LIMIT 1"
    )).fetchone()
    sleep_rows = await (await db.execute(
        "SELECT measured_at, sleep_stage FROM health_miband_activity "
        "WHERE source='mi_band_7' AND sleep_stage IN ('light','deep','rem') "
        "AND measured_at>=? ORDER BY measured_at ASC",
        (current - 7 * 86400,),
    )).fetchall()
    segments = []
    for row in sleep_rows:
        if not segments or row["measured_at"] - segments[-1][-1]["measured_at"] > 600:
            segments.append([])
        segments[-1].append(row)
    sleep_summary = None
    if segments:
        sessions = []
        for segment in segments:
            counts = {"light": 0, "deep": 0, "rem": 0}
            for row in segment:
                counts[row["sleep_stage"]] += 1
            sessions.append({
                "startAt": segment[0]["measured_at"],
                "endAt": segment[-1]["measured_at"] + 60,
                "totalMin": len(segment),
                "deepMin": counts["deep"],
                "lightMin": counts["light"],
                "remMin": counts["rem"],
            })
        latest_sleep_date = datetime.fromtimestamp(sessions[-1]["endAt"] - 1).date()
        day_sessions = [
            session for session in sessions
            if datetime.fromtimestamp(session["endAt"] - 1).date() == latest_sleep_date
        ]
        main_session = max(day_sessions, key=lambda session: session["totalMin"])
        classified_sessions = []
        for session in day_sessions:
            classified_sessions.append({
                "kind": "main" if session["totalMin"] >= 180 else "nap",
                **session,
            })
        sleep_summary = {
            "sleepDate": latest_sleep_date.isoformat(),
            "kind": "main" if main_session["totalMin"] >= 180 else "nap",
            "startAt": main_session["startAt"],
            "endAt": main_session["endAt"],
            "totalMin": sum(session["totalMin"] for session in day_sessions),
            "deepMin": sum(session["deepMin"] for session in day_sessions),
            "lightMin": sum(session["lightMin"] for session in day_sessions),
            "remMin": sum(session["remMin"] for session in day_sessions),
            "sessions": classified_sessions,
        }
    return {
        "mode": "ble",
        "sourceLabel": "Xiaomi Smart Band 7",
        "deviceName": latest["device_name"] if latest else "",
        "lastSyncAt": latest["synced_at"] if latest else 0,
        "latestSampleAt": latest["measured_at"] if latest else 0,
        "todaySteps": int(totals["steps"] if totals else 0),
        "stepsKind": "minute_samples",
        "activityMinutes": int(totals["active_minutes"] if totals else 0),
        **recent_activity,
        "supportsRecentActivity": True,
        "latestHeartRate": int(latest_heart["heart_rate"]) if latest_heart else None,
        "latestHeartRateAt": latest_heart["measured_at"] if latest_heart else 0,
        "heartRateKind": "minute_sample",
        "sleep": sleep_summary,
        "recentHeartRates": await _recent_mi_band_heart_rates(db, 20),
    }


@router.get("/summary")
async def get_health_summary():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM health_ring_latest WHERE id=1")
        ring = _row_dict(await cur.fetchone())
        heart_rates = await _recent_heart_rates(db, 20)
        heart_config = await get_heart_config(db)
        heart_events = await get_heart_events(db, 20)
        cur = await db.execute(
            "SELECT date, weight_kg, note, created_at, updated_at "
            "FROM health_weight_entries ORDER BY date DESC LIMIT 90"
        )
        weights = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT id, start_date, end_date, flow, symptoms, note, created_at, updated_at "
            "FROM health_period_entries ORDER BY start_date DESC LIMIT 24"
        )
        periods = [dict(r) for r in await cur.fetchall()]
        mi_band = await build_mi_band_summary(db)
    return {
        "ring": ring,
        "heartRates": heart_rates,
        "heartConfig": heart_config,
        "heartEvents": heart_events,
        "weights": weights,
        "periods": periods,
        "miBand": mi_band,
    }


@router.post("/ring/latest")
async def save_ring_latest(body: RingSnapshot):
    now = time.time()
    measured_at = body.measured_at or now
    sleep = body.sleep or RingSleep()
    raw_json = json.dumps(body.raw or {}, ensure_ascii=False)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_ring_latest (
                id, device_name, heart_rate, systolic_bp, diastolic_bp, spo2, hrv,
                measured_at, sleep_start_at, sleep_end_at, sleep_total_min,
                sleep_deep_min, sleep_light_min, sleep_rem_min, sleep_wake_min,
                sleep_wake_count, raw_json, synced_at
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                device_name=excluded.device_name,
                heart_rate=excluded.heart_rate,
                systolic_bp=excluded.systolic_bp,
                diastolic_bp=excluded.diastolic_bp,
                spo2=excluded.spo2,
                hrv=excluded.hrv,
                measured_at=excluded.measured_at,
                sleep_start_at=excluded.sleep_start_at,
                sleep_end_at=excluded.sleep_end_at,
                sleep_total_min=excluded.sleep_total_min,
                sleep_deep_min=excluded.sleep_deep_min,
                sleep_light_min=excluded.sleep_light_min,
                sleep_rem_min=excluded.sleep_rem_min,
                sleep_wake_min=excluded.sleep_wake_min,
                sleep_wake_count=excluded.sleep_wake_count,
                raw_json=excluded.raw_json,
                synced_at=excluded.synced_at
            """,
            (
                body.device_name.strip(),
                body.heart_rate,
                body.systolic_bp,
                body.diastolic_bp,
                body.spo2,
                body.hrv,
                measured_at,
                sleep.start_at,
                sleep.end_at,
                sleep.total_min,
                sleep.deep_min,
                sleep.light_min,
                sleep.rem_min,
                sleep.wake_min,
                sleep.wake_count,
                raw_json,
                now,
            ),
        )
        heart_entry = await _insert_heart_rate(
            db,
            device_name=body.device_name,
            heart_rate=body.heart_rate,
            measured_at=measured_at,
            source="snapshot",
            raw=body.raw,
        )
        heart_events_created = await analyze_heart_rate_entry(db, heart_entry)
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM health_ring_latest WHERE id=1")
        row = dict(await cur.fetchone())
        heart_rates = await _recent_heart_rates(db, 20)
        heart_events = await get_heart_events(db, 20)
    await manager.broadcast({"type": "health_ring_updated", "data": row})
    if heart_entry:
        await manager.broadcast({"type": "health_ring_heart_rates_updated", "items": heart_rates})
    if heart_events_created:
        await manager.broadcast({"type": "health_heart_events_updated", "items": heart_events})
        for event in heart_events_created:
            await manager.broadcast({"type": "health_heart_event_created", "data": event})
    return row


@router.get("/ring/heart-rates")
async def list_ring_heart_rates(limit: int = Query(20, ge=1, le=20)):
    async with get_db() as db:
        rows = await _recent_heart_rates(db, limit)
    return {"items": rows}


@router.post("/ring/request-diag")
async def request_ring_diag():
    await manager.broadcast({"type": "request_ring_diag"})
    return {"ok": True}


@router.post("/ring/diag-report")
async def ring_diag_report(body: RingDiagReport):
    _ring_diag_info["info"] = body.info
    _ring_diag_info["status"] = body.status
    _ring_diag_info["ts"] = time.time()
    print(f"[RingDiag] {body.status} {body.info}")
    await manager.broadcast({"type": "health_ring_diag", "data": _ring_diag_info})
    return {"ok": True}


@router.get("/ring/diag")
async def get_ring_diag():
    return _ring_diag_info


@router.get("/heart/config")
async def get_heart_config_route():
    return await get_heart_config()


@router.put("/heart/config")
async def update_heart_config_route(body: HeartConfigUpdate):
    result = await update_heart_config(body.dict(exclude_none=True))
    if "error" not in result:
        await manager.broadcast({"type": "health_heart_config_updated", "data": result})
    return result


@router.get("/heart/events")
async def list_heart_events(limit: int = Query(20, ge=1, le=100)):
    return {"items": await get_heart_events(limit=limit)}


async def _save_heart_rate_sample(body: HeartRateSample, default_source: str):
    if not _valid_heart_rate(body.heart_rate):
        return {"error": "心率数值不正确"}
    now = time.time()
    measured_at = body.measured_at or now
    raw_json = json.dumps(body.raw or {}, ensure_ascii=False)
    async with get_db() as db:
        entry = await _insert_heart_rate(
            db,
            device_name=body.device_name,
            heart_rate=body.heart_rate,
            measured_at=measured_at,
            source=body.source or default_source,
            raw=body.raw,
        )
        heart_events_created = (
            await analyze_heart_rate_entry(db, entry)
            if entry and entry.get("is_new")
            else []
        )
        await db.execute(
            """
            INSERT INTO health_ring_latest (
                id, device_name, heart_rate, measured_at, raw_json, synced_at
            ) VALUES (1,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                device_name=CASE
                    WHEN excluded.device_name != '' THEN excluded.device_name
                    ELSE health_ring_latest.device_name
                END,
                heart_rate=excluded.heart_rate,
                measured_at=excluded.measured_at,
                raw_json=excluded.raw_json,
                synced_at=excluded.synced_at
            """,
            (
                (body.device_name or "").strip(),
                int(body.heart_rate),
                measured_at,
                raw_json,
                now,
            ),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM health_ring_latest WHERE id=1")
        ring = dict(await cur.fetchone())
        heart_rates = await _recent_heart_rates(db, 20)
        heart_events = await get_heart_events(db, 20)
    await manager.broadcast({"type": "health_ring_updated", "data": ring})
    await manager.broadcast({"type": "health_ring_heart_rates_updated", "items": heart_rates})
    if heart_events_created:
        await manager.broadcast({"type": "health_heart_events_updated", "items": heart_events})
        for event in heart_events_created:
            await manager.broadcast({"type": "health_heart_event_created", "data": event})
    return {"ring": ring, "entry": entry, "items": heart_rates, "events": heart_events_created, "heartEvents": heart_events}


@router.post("/heart-rate")
async def save_heart_rate(body: HeartRateSample):
    return await _save_heart_rate_sample(body, "wearable_realtime")


@router.post("/mi-band/activity-batch")
async def save_mi_band_activity_batch(body: MiBandActivityBatch):
    now = time.time()
    device_name = body.device_name.strip() or "Xiaomi Smart Band 7"
    heart_events_created = []
    async with get_db() as db:
        for sample in body.samples:
            stage = sample.sleep_stage if sample.sleep_stage in {"light", "deep", "rem"} else ""
            await db.execute(
                """
                INSERT INTO health_miband_activity (
                    source, measured_at, device_name, raw_kind, intensity, steps,
                    heart_rate, unknown_value, sleep_value, deep_sleep_value,
                    rem_sleep_value, sleep_stage, synced_at
                ) VALUES ('mi_band_7',?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, measured_at) DO UPDATE SET
                    device_name=excluded.device_name,
                    raw_kind=excluded.raw_kind,
                    intensity=excluded.intensity,
                    steps=excluded.steps,
                    heart_rate=excluded.heart_rate,
                    unknown_value=excluded.unknown_value,
                    sleep_value=excluded.sleep_value,
                    deep_sleep_value=excluded.deep_sleep_value,
                    rem_sleep_value=excluded.rem_sleep_value,
                    sleep_stage=excluded.sleep_stage,
                    synced_at=excluded.synced_at
                """,
                (
                    float(sample.measured_at), device_name, int(sample.raw_kind),
                    max(0, int(sample.intensity)), max(0, int(sample.steps)),
                    int(sample.heart_rate), int(sample.unknown), int(sample.sleep),
                    int(sample.deep_sleep), int(sample.rem_sleep), stage, now,
                ),
            )
            if _valid_heart_rate(sample.heart_rate):
                raw = {
                    "raw_kind": sample.raw_kind,
                    "intensity": sample.intensity,
                    "steps": sample.steps,
                    "unknown": sample.unknown,
                    "sleep": sample.sleep,
                    "deep_sleep": sample.deep_sleep,
                    "rem_sleep": sample.rem_sleep,
                }
                if stage:
                    raw["sleep_stage"] = stage
                entry = await _insert_heart_rate(
                    db, device_name=device_name, heart_rate=sample.heart_rate,
                    measured_at=sample.measured_at, source="mi_band_7", raw=raw,
                )
                if entry and entry.get("is_new"):
                    heart_events_created.extend(await analyze_heart_rate_entry(db, entry))
        await db.execute(
            "DELETE FROM health_miband_activity WHERE source='mi_band_7' AND measured_at < ?",
            (now - 45 * 86400,),
        )
        latest_heart = next(
            (sample for sample in reversed(body.samples) if _valid_heart_rate(sample.heart_rate)),
            None,
        )
        if latest_heart is not None:
            await db.execute(
                """
                INSERT INTO health_ring_latest(id, device_name, heart_rate, measured_at, raw_json, synced_at)
                VALUES(1,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    device_name=excluded.device_name,
                    heart_rate=excluded.heart_rate,
                    measured_at=excluded.measured_at,
                    raw_json=excluded.raw_json,
                    synced_at=excluded.synced_at
                """,
                (device_name, latest_heart.heart_rate, latest_heart.measured_at, "{}", now),
            )
        await db.commit()
        summary = await build_mi_band_summary(db, now=now)
        heart_events = await get_heart_events(db, 20)
    await manager.broadcast({"type": "health_mi_band_updated", "data": summary})
    await manager.broadcast({"type": "health_ring_heart_rates_updated", "items": summary["recentHeartRates"]})
    if heart_events_created:
        await manager.broadcast({"type": "health_heart_events_updated", "items": heart_events})
    return {"accepted": len(body.samples), "miBand": summary}


@router.post("/ring/heart-rate")
async def save_ring_heart_rate(body: RingHeartRate):
    return await _save_heart_rate_sample(body, "realtime")


@router.get("/weights")
async def list_weights(
    start: str = Query("", max_length=10),
    end: str = Query("", max_length=10),
    limit: int = Query(180, ge=1, le=1000),
):
    where = []
    params: list[object] = []
    if start and _valid_date(start):
        where.append("date >= ?")
        params.append(start)
    if end and _valid_date(end):
        where.append("date <= ?")
        params.append(end)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT date, weight_kg, note, created_at, updated_at "
            f"FROM health_weight_entries {where_sql} ORDER BY date DESC LIMIT ?",
            [*params, limit],
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.post("/weights")
async def upsert_weight(body: WeightEntry):
    if not _valid_date(body.date):
        return {"error": "日期格式不正确"}
    if body.weight_kg <= 0 or body.weight_kg > 500:
        return {"error": "体重数值不正确"}
    now = time.time()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_weight_entries (date, weight_kg, note, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                weight_kg=excluded.weight_kg,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (body.date, body.weight_kg, body.note.strip(), now, now),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT date, weight_kg, note, created_at, updated_at FROM health_weight_entries WHERE date=?",
            (body.date,),
        )
        row = dict(await cur.fetchone())
    await manager.broadcast({"type": "health_weight_updated", "data": row})
    return row


@router.delete("/weights/{entry_date}")
async def delete_weight(entry_date: str):
    if not _valid_date(entry_date):
        return {"error": "日期格式不正确"}
    async with get_db() as db:
        await db.execute("DELETE FROM health_weight_entries WHERE date=?", (entry_date,))
        await db.commit()
    return {"ok": True}


@router.get("/periods")
async def list_periods(limit: int = Query(36, ge=1, le=200)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, start_date, end_date, flow, symptoms, note, created_at, updated_at "
            "FROM health_period_entries ORDER BY start_date DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.post("/periods")
async def upsert_period(body: PeriodEntry):
    if not _valid_date(body.start_date):
        return {"error": "开始日期格式不正确"}
    if body.end_date and not _valid_date(body.end_date):
        return {"error": "结束日期格式不正确"}
    if body.end_date and body.end_date < body.start_date:
        return {"error": "结束日期不能早于开始日期"}
    now = time.time()
    entry_id = body.id.strip() or f"hp_{int(now * 1000)}"
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_period_entries
                (id, start_date, end_date, flow, symptoms, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                flow=excluded.flow,
                symptoms=excluded.symptoms,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                entry_id,
                body.start_date,
                body.end_date,
                body.flow.strip(),
                body.symptoms.strip(),
                body.note.strip(),
                now,
                now,
            ),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, start_date, end_date, flow, symptoms, note, created_at, updated_at "
            "FROM health_period_entries WHERE id=?",
            (entry_id,),
        )
        row = dict(await cur.fetchone())
    await manager.broadcast({"type": "health_period_updated", "data": row})
    return row


@router.delete("/periods/{entry_id}")
async def delete_period(entry_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM health_period_entries WHERE id=?", (entry_id,))
        await db.commit()
    return {"ok": True}


@router.post("/mi-band/cloud-sync")
async def sync_mi_cloud():
    """手动触发一次小米云端亲友健康同步（需先配好小号 token + 亲友）。"""
    try:
        from mi_cloud_health import sync_mi_cloud_now
        result = await sync_mi_cloud_now()
        async with get_db() as db:
            summary = await build_mi_band_summary(db)
        await manager.broadcast({"type": "health_mi_band_updated", "data": summary})
        if summary.get("recentHeartRates"):
            await manager.broadcast({"type": "health_ring_heart_rates_updated", "items": summary["recentHeartRates"]})
        return {"ok": True, "result": result, "miBand": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/mi-band/cloud-relatives")
async def list_mi_cloud_relatives():
    """列出小号当前所有亲友（用于查出大号的 UID 填 settings.mi_cloud.relative_uid）。"""
    try:
        from mi_cloud_health import TOKEN_PATH
        if not TOKEN_PATH.exists():
            return {"ok": False, "error": "token 不存在，先跑 mi_cloud_login.py"}
        from mi_fitness import MiHealthClient
        async with MiHealthClient.from_token(str(TOKEN_PATH)) as client:
            relatives = await client.get_relatives()
        return {"ok": True, "relatives": [
            {"uid": r.relative_uid, "note": r.relative_note} for r in relatives
        ]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
