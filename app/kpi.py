"""기간 해석 + KPI 집계 (설계서 §1-6, §1-8)."""
import calendar
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select

from .config import CFG
from .db import CollectLog, RawOrder, RobotOrderNote

DONE = "DROP_FINISHED"
ROBOT = "로봇연계"


# ---------------------------------------------------------------- 기간
@dataclass
class Period:
    grain: str
    start: date
    end: date            # 기간 정의상의 끝
    eff_end: date        # 데이터가 존재할 수 있는 마지막 날 (D-1 적용)
    in_progress: bool
    elapsed_days: int
    label: str
    prev: str | None
    next: str | None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _shift_month(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


def data_end(today: date) -> date:
    return today - timedelta(days=1) if CFG.get("exclude_today", True) else today


def resolve_period(grain: str, anchor: date | None, start: date | None, end: date | None,
                   today: date | None = None) -> Period:
    today = today or date.today()
    de = data_end(today)
    if grain == "day":
        a = anchor or de
        s = e = a
        prev, nxt = (a - timedelta(days=1)).isoformat(), (a + timedelta(days=1)).isoformat()
        label = f"{a:%Y-%m-%d} ({'월화수목금토일'[a.weekday()]})"
    elif grain == "month":
        a = anchor or today
        s = date(a.year, a.month, 1)
        e = date(a.year, a.month, calendar.monthrange(a.year, a.month)[1])
        prev, nxt = _shift_month(s, -1).isoformat(), _shift_month(s, 1).isoformat()
        label = f"{s:%Y년 %m월}"
    elif grain == "custom":
        s = start or de - timedelta(days=6)
        e = end or de
        if e < s:
            s, e = e, s
        prev = nxt = None
        label = f"{s:%Y-%m-%d} ~ {e:%Y-%m-%d}"
    else:
        grain = "week"
        a = anchor or today
        s = _monday(a)
        e = s + timedelta(days=6)
        prev, nxt = (s - timedelta(days=7)).isoformat(), (s + timedelta(days=7)).isoformat()
        label = f"{s:%m/%d} ~ {e:%m/%d} 주"
    eff_end = min(e, de)
    elapsed = max((eff_end - s).days + 1, 0)
    return Period(grain, s, e, eff_end, e > de, elapsed, label, prev, nxt)


# ---------------------------------------------------------------- 조회
def _bounds(s: date, e: date):
    return datetime.combine(s, datetime.min.time()), datetime.combine(e + timedelta(days=1), datetime.min.time())


def scoped_done(session, s: date, e: date) -> list[RawOrder]:
    """집계 모수: 단지 + 상점구분(로드샵·B2B) + 완료."""
    if e < s:
        return []
    lo, hi = _bounds(s, e)
    q = select(RawOrder).where(
        RawOrder.building == CFG["building"], RawOrder.store_type.in_(CFG["store_types"]),
        RawOrder.delivery_status == DONE, RawOrder.ord_dt >= lo, RawOrder.ord_dt < hi)
    return list(session.scalars(q))


def load_notes(session) -> dict[str, RobotOrderNote]:
    return {n.delivery_id: n for n in session.scalars(select(RobotOrderNote))}


def freshness(session) -> dict:
    ok = session.scalars(select(CollectLog).where(CollectLog.status == "success")
                         .order_by(CollectLog.ts.desc())).first()
    last = session.scalars(select(CollectLog).order_by(CollectLog.ts.desc())).first()
    return {
        "last_success": ok.ts if ok else None,
        "last_failed": bool(last and last.status == "fail"),
        "last_message": last.message if last else None,
    }


# ---------------------------------------------------------------- 집계
def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _min(sec):
    return None if sec is None else round(sec / 60, 1)


def _pickup_to_finish(o: RawOrder):
    if o.s_order_finish is None or o.s_order_pickup is None:
        return None
    return o.s_order_finish - o.s_order_pickup


def _excluded(o, notes) -> bool:
    n = notes.get(o.delivery_id)
    return bool(n and n.exclude_from_kpi)


def _time_invalid(o, notes) -> bool:
    n = notes.get(o.delivery_id)
    return bool(n and n.result_type == "완료(오류)")


def keep(rows: list[RawOrder], notes: dict) -> list[RawOrder]:
    """집계 제외 체크된 건을 뺀다(모든 지표·차트 공통)."""
    return [o for o in rows if not _excluded(o, notes)]


def compute_metrics(rows: list[RawOrder], notes: dict) -> dict:
    excluded_n = len(rows) - len(keep(rows, notes))
    # 특이사항 집계는 제외 건까지 포함한 전체 기준 (기록된 건을 한눈에 보기 위함)
    ann = [notes[o.delivery_id] for o in rows if o.delivery_id in notes]
    ann = [n for n in ann if n.note or n.exclude_from_kpi or n.miss_reason
           or (n.result_type and n.result_type != "정상")]
    annot = {
        "annotated_n": len(ann),
        "noted_n": sum(1 for n in ann if n.note),
        "fail_n": sum(1 for n in ann if n.result_type == "실패"),
        "other_n": sum(1 for n in ann if n.result_type == "기타"),
        "err_n": sum(1 for n in ann if n.result_type == "완료(오류)"),
        "miss_n": sum(1 for n in ann if n.miss_reason),
    }
    rows = keep(rows, notes)
    robots_all = [o for o in rows if o.delivery_type == ROBOT]
    general = [o for o in rows if o.delivery_type != ROBOT]
    loadshop_general = [o for o in general if o.store_type == "일반(로드샵)"]
    b2b_general = [o for o in general if o.store_type == "B2B"]
    robots = [o for o in robots_all if not _time_invalid(o, notes)]   # 시간·적시 지표 모수
    timed = general + robots

    total_secs = [o.s_order_finish for o in timed]
    pick_secs = [_pickup_to_finish(o) for o in timed]
    thr = CFG["timely_threshold_sec"]

    elig = [o for o in robots if o.s_dockclose_robotfinish is not None]
    timely_n = sum(1 for o in elig if o.s_dockclose_robotfinish <= thr)

    def rtype(o):
        n = notes.get(o.delivery_id)
        return (n.result_type if n and n.result_type else "정상")

    succ_base = [o for o in robots_all if rtype(o) != "기타"]            # 성공률은 완료(오류)도 포함
    succ_n = sum(1 for o in succ_base if rtype(o) in ("정상", "완료(오류)"))

    base = CFG["baseline"]
    avg_total = _mean(total_secs)
    avg_pick = _mean(pick_secs)
    clean = [v for v in total_secs if v is not None]

    seg_robot = {
        "접수→배차": _min(_mean([o.s_order_dispatch for o in robots])),
        "배차→픽업완료": _min(_mean([o.s_dispatch_pickup for o in robots])),
        "픽업완료→적재함닫힘": _min(_mean([o.s_pickup_dockclose for o in robots])),
        "적재함닫힘→로봇배송완료": _min(_mean([o.s_dockclose_robotfinish for o in robots])),
        "픽업→완료": None,
    }
    seg_general = {
        "접수→배차": _min(_mean([o.s_order_dispatch for o in general])),
        "배차→픽업완료": _min(_mean([o.s_dispatch_pickup for o in general])),
        "픽업완료→적재함닫힘": None,
        "적재함닫힘→로봇배송완료": None,
        "픽업→완료": _min(_mean([_pickup_to_finish(o) for o in general])),
    }

    return {
        "completed": len(rows),
        "robot_done": len(robots_all),
        "general_done": len(general),
        "loadshop_done": len(loadshop_general),
        "b2b_done": len(b2b_general),
        "usage_pct": round(len(robots_all) / len(rows) * 100, 1) if rows else None,
        "avg_total_min": _min(avg_total),
        "median_total_min": _min(statistics.median(clean)) if clean else None,
        "avg_pickup_min": _min(avg_pick),
        "avg_total_general_min": _min(_mean([o.s_order_finish for o in general])),
        "avg_total_robot_min": _min(_mean([o.s_order_finish for o in robots])),
        "delta_total_min": round(_min(avg_total) - base["total_min"], 1) if avg_total is not None else None,
        "delta_pickup_min": round(_min(avg_pick) - base["pickup_min"], 1) if avg_pick is not None else None,
        "timely_n": timely_n, "timely_base": len(elig),
        "timely_pct": round(timely_n / len(elig) * 100, 1) if elig else None,
        "success_n": succ_n, "success_base": len(succ_base),
        "success_pct": round(succ_n / len(succ_base) * 100, 1) if succ_base else None,
        "excluded_n": excluded_n,
        "time_n": sum(1 for v in total_secs if v is not None),
        "time_excluded_n": len(robots_all) - len(robots),
        **annot,
        "seg_robot": seg_robot, "seg_general": seg_general,
    }


def _days(s: date, e: date):
    d = s
    while d <= e:
        yield d
        d += timedelta(days=1)


def daily_series(rows, s: date, e: date) -> dict:
    days = list(_days(s, e)) if e >= s else []
    gen = {d: 0 for d in days}
    rob = {d: 0 for d in days}
    for o in rows:
        d = o.ord_dt.date()
        if d in gen:
            (rob if o.delivery_type == ROBOT else gen)[d] += 1
    return {"labels": [f"{d:%m/%d}" for d in days],
            "general": [gen[d] for d in days], "robot": [rob[d] for d in days]}


def weekly_series(rows, notes, de: date, weeks: int = 12) -> dict:
    last = _monday(de)
    starts = [last - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    out = {"labels": [], "robot_min": [], "general_min": [], "usage_pct": [], "n": []}
    for ws in starts:
        we = ws + timedelta(days=6)
        wr = [o for o in rows if ws <= o.ord_dt.date() <= we]
        rob_all = [o for o in wr if o.delivery_type == ROBOT]
        rob = [o for o in rob_all if not _time_invalid(o, notes)]       # 시간 평균에서만 제외
        gen = [o for o in wr if o.delivery_type != ROBOT]
        out["labels"].append(f"{ws:%m/%d}~" + ("" if we <= de else "(진행중)"))
        out["robot_min"].append(_min(_mean([o.s_order_finish for o in rob])))
        out["general_min"].append(_min(_mean([o.s_order_finish for o in gen])))
        out["usage_pct"].append(round(len(rob_all) / len(wr) * 100, 1) if wr else None)
        out["n"].append(len(wr))
    return out


def heatmap(rows, s: date, e: date) -> dict:
    days = list(_days(s, e)) if e >= s else []
    cnt = {}
    for o in rows:
        cnt[(o.ord_dt.date(), o.ord_dt.hour)] = cnt.get((o.ord_dt.date(), o.ord_dt.hour), 0) + 1
    hours = sorted({h for (_, h) in cnt}) if cnt else []
    if hours:
        hours = list(range(min(hours), max(hours) + 1))
    mx = max(cnt.values()) if cnt else 0
    return {"days": [f"{d:%m/%d}" for d in days], "hours": hours,
            "cells": [[cnt.get((d, h), 0) for d in days] for h in hours], "max": mx}


def daily_table(rows, notes, s: date, e: date) -> list[dict]:
    """일자별 주요 항목 (최신 일자가 위). 지표 정의는 카드와 동일(compute_metrics)."""
    by_day: dict[date, list] = {}
    for o in rows:
        by_day.setdefault(o.ord_dt.date(), []).append(o)
    out = []
    for d in _days(s, e):
        m = compute_metrics(by_day.get(d, []), notes)
        out.append({"date": d, "m": m})
    return out[::-1]


def _pctile(vals, q: float):
    """선형보간 백분위수 (vals: 초 단위, None 제외)."""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    f = int(k)
    return v[f] + (v[min(f + 1, len(v) - 1)] - v[f]) * (k - f)


def day_detail(rows, notes, day: date) -> dict:
    """일 단위 상세: 피크 시간대, 시간대별 건수·평균시간, 시간 분포(P90·최대·최소), 지연 건수, 로봇 적시 미달 건."""
    rows = keep([o for o in rows if o.ord_dt.date() == day], notes)
    robots = [o for o in rows if o.delivery_type == ROBOT]
    timed = [o for o in rows if not (o.delivery_type == ROBOT and _time_invalid(o, notes))]
    secs = [o.s_order_finish for o in timed if o.s_order_finish is not None]

    hours = list(range(min(o.ord_dt.hour for o in rows), max(o.ord_dt.hour for o in rows) + 1)) if rows else []
    cnt = {h: 0 for h in hours}
    robot_cnt = {h: 0 for h in hours}
    for o in rows:
        cnt[o.ord_dt.hour] += 1
        if o.delivery_type == ROBOT:
            robot_cnt[o.ord_dt.hour] += 1
    hourly = {
        "hours": [f"{h:02d}시" for h in hours],
        "general": [cnt[h] - robot_cnt[h] for h in hours],
        "robot": [robot_cnt[h] for h in hours],
        "avg_min": [_min(_mean([o.s_order_finish for o in timed if o.ord_dt.hour == h])) for h in hours],
    }
    peak = max(hours, key=lambda h: (cnt[h], -h)) if hours else None

    base_sec = CFG["baseline"]["total_min"] * 60
    thr = CFG["timely_threshold_sec"]
    delayed = [s for s in secs if s > base_sec]
    late = sorted((o for o in robots if not _time_invalid(o, notes)
                   and o.s_dockclose_robotfinish is not None and o.s_dockclose_robotfinish > thr),
                  key=lambda o: o.ord_dt)
    return {
        "hourly": hourly,
        "peak_hour": peak, "peak_n": cnt[peak] if peak is not None else None,
        "p90_min": _min(_pctile(secs, 0.9)), "max_min": _min(max(secs)) if secs else None,
        "min_min": _min(min(secs)) if secs else None, "time_n": len(secs),
        "delay_n": len(delayed), "delay_pct": round(len(delayed) / len(secs) * 100, 1) if secs else None,
        "late_robots": [row_view(o, notes.get(o.delivery_id)) for o in late],
    }


def dashboard(session, p: Period) -> dict:
    notes = load_notes(session)
    rows = scoped_done(session, p.start, p.eff_end)
    m = compute_metrics(rows, notes)            # 내부에서 제외 건 반영

    de = data_end(date.today())
    # 트렌드 구간: 일 단위는 최근 14일, 그 외는 선택 기간
    if p.grain == "day":
        ts, te = p.start - timedelta(days=13), p.eff_end
    else:
        ts, te = p.start, p.eff_end
    trend_all = scoped_done(session, ts, te)
    trend_rows = keep(trend_all, notes)
    long_rows = keep(scoped_done(session, _monday(de) - timedelta(weeks=11), de), notes)
    return {
        "metrics": m,
        "daily": daily_series(trend_rows, ts, te),
        "weekly": weekly_series(long_rows, notes, de),
        "heat": heatmap(trend_rows, ts, te),
        "day_rows": daily_table(trend_all, notes, ts, te),
        "dd": day_detail(trend_all, notes, p.start) if p.grain == "day" else None,
        "trend_range": (ts, te),
    }


# ---------------------------------------------------------------- 로봇 건 리스트
ERR = "완료(오류)"      # 로봇은 정상 배송했으나 시스템 이벤트 오류로 시간 데이터가 비정상 → 성공엔 포함, 시간 지표에서 제외
RESULT_TYPES = ["정상", ERR, "실패", "기타"]


def robot_list(session, p: Period, flt: str | None = None) -> list[dict]:
    if p.eff_end < p.start:
        return []
    lo, hi = _bounds(p.start, p.eff_end)
    q = (select(RawOrder).where(RawOrder.building == CFG["building"],
                                RawOrder.delivery_type == ROBOT,
                                RawOrder.ord_dt >= lo, RawOrder.ord_dt < hi)
         .order_by(RawOrder.ord_dt.desc()))
    notes = load_notes(session)
    out = []
    for o in session.scalars(q):
        n = notes.get(o.delivery_id)
        row = row_view(o, n)
        out.append(row)
    if flt == "noted":
        out = [r for r in out if r["note"]]
    elif flt == "excluded":
        out = [r for r in out if r["exclude"]]
    elif flt == "error":
        out = [r for r in out if r["result_type"] == ERR]
    elif flt == "failed":
        out = [r for r in out if r["result_type"] == "실패"]
    return out


def row_view(o: RawOrder, n: RobotOrderNote | None) -> dict:
    thr = CFG["timely_threshold_sec"]
    hand = o.s_dockclose_robotfinish
    return {
        "delivery_id": o.delivery_id,
        "order_id": o.order_id,
        "robot_delivery_id": o.robot_delivery_id,
        "ord_dt": o.ord_dt,
        "store_type": o.store_type,
        "store_name": o.store_name,
        "order_source": o.order_source,
        "robot_name": o.robot_name,
        "total_min": _min(o.s_order_finish),
        "hand_min": _min(hand),
        "late": (hand is not None and hand > thr),
        "dispatch_count": o.dispatch_count,
        "delivery_status": o.delivery_status,
        "in_scope": o.store_type in CFG["store_types"],
        "result_type": (n.result_type if n and n.result_type else ""),
        "note": (n.note if n and n.note else ""),
        "exclude": bool(n and n.exclude_from_kpi),
        "time_excluded": bool(n and n.result_type == ERR),
        "updated_at": n.updated_at if n else None,
        "updated_by": n.updated_by if n else None,
    }


# ---------------------------------------------------------------- 전체 주문 (로봇 누락 파악)
MISS_REASONS = ["시스템 오류(로봇 매칭 실패)", "테스트 주문", "로봇 운영 외(점검·미운영)", "상점 미동의", "기타"]
PER_PAGE = 100


def order_row_view(o: RawOrder, n: RobotOrderNote | None) -> dict:
    return {
        "delivery_id": o.delivery_id, "order_id": o.order_id, "ord_dt": o.ord_dt,
        "delivery_type": o.delivery_type, "is_robot": o.delivery_type == ROBOT,
        "robot_name": o.robot_name, "order_source": o.order_source, "store_name": o.store_name,
        "store_type": o.store_type, "delivery_status": o.delivery_status,
        "total_min": _min(o.s_order_finish), "dispatch_count": o.dispatch_count,
        "in_scope": o.store_type in CFG["store_types"],
        "miss_reason": (n.miss_reason if n and n.miss_reason else ""),
        "note": (n.note if n and n.note else ""),
        "exclude": bool(n and n.exclude_from_kpi),
        "updated_at": n.updated_at if n else None, "updated_by": n.updated_by if n else None,
    }


def order_list(session, p: Period, flt: str | None = None, st: str | None = None,
               q: str | None = None, page: int = 1) -> dict:
    if p.eff_end < p.start:
        return {"rows": [], "total": 0, "pages": 1, "page": 1, "counts": {}}
    lo, hi = _bounds(p.start, p.eff_end)
    stmt = (select(RawOrder).where(RawOrder.building == CFG["building"],
                                   RawOrder.ord_dt >= lo, RawOrder.ord_dt < hi)
            .order_by(RawOrder.ord_dt.desc()))
    notes = load_notes(session)
    rows = [order_row_view(o, notes.get(o.delivery_id)) for o in session.scalars(stmt)]
    if st == "loadshop":
        rows = [r for r in rows if r["store_type"] == "일반(로드샵)"]
    elif st == "b2b":
        rows = [r for r in rows if r["store_type"] == "B2B"]
    if q:
        k = q.strip().lower()
        rows = [r for r in rows if any(k in str(r[f] or "").lower() for f in
                                       ("order_id", "delivery_id", "store_name", "order_source", "robot_name"))]

    def is_miss(r):      # 로봇 안 탄 모수 내 완료 건 = 로봇 누락 후보
        return not r["is_robot"] and r["in_scope"] and r["delivery_status"] == DONE

    counts = {
        "all": len(rows),
        "robot": sum(1 for r in rows if r["is_robot"]),
        "miss": sum(1 for r in rows if is_miss(r)),
        "noted": sum(1 for r in rows if r["miss_reason"] or r["note"] or r["exclude"]),
        "excluded": sum(1 for r in rows if r["exclude"]),
    }
    if flt == "robot":
        rows = [r for r in rows if r["is_robot"]]
    elif flt == "miss":
        rows = [r for r in rows if is_miss(r)]
    elif flt == "noted":
        rows = [r for r in rows if r["miss_reason"] or r["note"] or r["exclude"]]
    elif flt == "excluded":
        rows = [r for r in rows if r["exclude"]]
    total = len(rows)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(max(page, 1), pages)
    return {"rows": rows[(page - 1) * PER_PAGE: page * PER_PAGE], "total": total,
            "pages": pages, "page": page, "counts": counts}
