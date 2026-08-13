# -*- coding: utf-8 -*-
"""生理期预测 —— 纯函数，无 DB 依赖，无随机，全部可复现。

调用方传入 period 列表（每条含 start_date 字符串，可选 end_date），
predict_next 返回下次预计日期、排卵窗口、距今天数、逾期与否等。

算法：
- 周期长度 = 相邻两条 start_date 之差（天）。
- 取最近 ≤6 个周期长度；≥4 个时去掉一个最大、一个最小（剔除异常）。
- 剩余做指数加权平均（越近权重越大，0.7 衰减）。
- 下次预计 start = 最后一条 start_date + 平均周期长度。
- 排卵 = 下次预计 start - 14（Ogino-Knaus），窗口 ±2 天。
"""

from __future__ import annotations

from datetime import date, timedelta


def _parse(d: str) -> date | None:
    if not d:
        return None
    try:
        return date.fromisoformat(str(d)[:10])
    except Exception:
        return None


def predict_next(periods: list[dict], today: str | None = None) -> dict:
    """根据历史 period 列表预测下次生理期。

    periods: [{"start_date": "YYYY-MM-DD", "end_date": "...", ...}, ...]
        顺序任意，内部按 start_date 升序。
    today: "YYYY-MM-DD"，不传用 date.today()。
    """
    today_d = _parse(today) if today else date.today()

    # 过滤掉无 start_date 的脏数据，按 start 升序
    items = []
    for p in (periods or []):
        s = _parse((p or {}).get("start_date"))
        if s is not None:
            items.append((s, p))
    items.sort(key=lambda x: x[0])

    if len(items) < 2:
        return {"ok": False, "reason": "记录不足", "sample_count": len(items)}

    starts = [it[0] for it in items]
    # 相邻周期长度（天）
    gaps = [(starts[i + 1] - starts[i]).days for i in range(len(starts) - 1)]
    if not gaps:
        return {"ok": False, "reason": "记录不足", "sample_count": len(items)}

    recent = gaps[-6:]
    used = recent
    if len(recent) >= 4:
        # 去一个最大、一个最小
        s = sorted(recent)
        used = s[1:-1]

    # 指数衰减加权：越近权重越大
    weights = [0.7 ** (len(used) - 1 - i) for i in range(len(used))]
    wsum = sum(weights)
    cycle_avg = sum(w * g for w, g in zip(weights, used)) / wsum
    cycle_avg_int = int(round(cycle_avg))

    last_start = starts[-1]
    next_start = last_start + timedelta(days=cycle_avg_int)

    ovulation = next_start - timedelta(days=14)
    ov_window = [ovulation - timedelta(days=2), ovulation + timedelta(days=2)]

    days_until = (next_start - today_d).days

    overdue = days_until < 0
    soon = 0 <= days_until <= 3

    return {
        "ok": True,
        "next_start": next_start.isoformat(),
        "days_until": days_until,
        "overdue": overdue,
        "soon": soon,
        "ovulation_date": ovulation.isoformat(),
        "ovulation_window": [ov_window[0].isoformat(), ov_window[1].isoformat()],
        "cycle_length_avg": cycle_avg_int,
        "sample_count": len(items),
        "last_start": last_start.isoformat(),
    }


def format_prediction_for_prompt(pred: dict) -> str:
    """把预测结果拼成可塞进 AI 上下文/指令的中文片段。"""
    if not pred or not pred.get("ok"):
        return ""
    parts = []
    parts.append(f"下次预计:{pred['next_start']}(距今{pred['days_until']}天)")
    parts.append(
        f"排卵期:{pred['ovulation_window'][0]}~{pred['ovulation_window'][1]}"
    )
    if pred.get("overdue"):
        parts.append(f"已逾期{-pred['days_until']}天还没记")
    elif pred.get("soon"):
        parts.append("快到了")
    return " ".join(parts)
