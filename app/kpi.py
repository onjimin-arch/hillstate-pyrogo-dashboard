"""기간 해석 + KPI 집계 (설계서 §1-6, §1-8)."""
import calendar
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from .config import CFG
from .db import CollectLog, RawOrder, RobotOrderNote, RobotTrip

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


def _med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _min(sec):
    return None if sec is None else round(sec / 60, 1)


def _tail_sec(o: RawOrder):
    """배달완료 직전 구간(초). 로봇 건은 로봇배송완료→완료, 일반 건은 픽업완료→완료."""
    prev = o.robot_finish_dt if o.delivery_type == ROBOT else o.pickup_dt
    if prev is None or o.finish_dt is None:
        return None
    return (o.finish_dt - prev).total_seconds()


# 로봇 건은 로봇배송완료와 배달완료가 사실상 같은 시각에 찍힌다(관측상 1~2초 차, 역전도 있음).
# 이 이내면 숫자 대신 '동시'로 표기한다 — "-0.0" 으로 보이는 것도, 0.0(측정됐는데 0분)으로
# 보이는 것도 구조상 그런 것인지 구분이 안 되기 때문이다.
TAIL_INSTANT_SEC = 10


def _is_instant(sec) -> bool:
    return sec is not None and abs(sec) <= TAIL_INSTANT_SEC


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


LOADSHOP = "일반(로드샵)"
CONSENT_OK, CONSENT_NO, CONSENT_NONE = "동의", "미동의", "미응답"


def consent_stats(rows: list[RawOrder]) -> dict:
    """로드샵 상점의 로봇배송 동의 현황. 상점ID 기준(상점 정보 없는 '0' 제외)이며 값은 주문이 가장 늦은 건 기준.
    store_robot_consent 가 전부 NULL(컬럼 추가 전 수집분)이면 비율은 None."""
    shop = {}
    orders = [o for o in rows if o.store_type == LOADSHOP and o.store_robot_consent]
    for o in sorted(orders, key=lambda o: o.ord_dt):
        if o.store_id and o.store_id != "0":
            shop[o.store_id] = o.store_robot_consent
    n = {k: sum(1 for v in shop.values() if v == k) for k in (CONSENT_OK, CONSENT_NO, CONSENT_NONE)}
    ok_orders = sum(1 for o in orders if o.store_robot_consent == CONSENT_OK)
    return {
        "consent_shops": n[CONSENT_OK], "noconsent_shops": n[CONSENT_NO], "noresp_shops": n[CONSENT_NONE],
        "consent_shop_base": len(shop),
        "consent_shop_pct": round(n[CONSENT_OK] / len(shop) * 100, 1) if shop else None,
        "consent_order_n": ok_orders, "consent_order_base": len(orders),
        "consent_order_pct": round(ok_orders / len(orders) * 100, 1) if orders else None,
    }


def unfinished_robots(session, s: date, e: date) -> list[RawOrder]:
    """미완료(취소 등) 로봇연계 건. KPI 모수는 아니지만 로봇 건 리스트에는 보이므로 특이사항 카드 집계에만 쓴다."""
    if e < s:
        return []
    lo, hi = _bounds(s, e)
    q = select(RawOrder).where(
        RawOrder.building == CFG["building"], RawOrder.store_type.in_(CFG["store_types"]),
        RawOrder.delivery_type == ROBOT, RawOrder.delivery_status != DONE,
        RawOrder.ord_dt >= lo, RawOrder.ord_dt < hi)
    return list(session.scalars(q))


def annotation_counts(rows: list[RawOrder], notes: dict) -> dict:
    """특이사항·집계 제외 카드 집계. 제외 건까지 포함한 전체 기준 (기록된 건을 한눈에 보기 위함)."""
    ann = [notes[o.delivery_id] for o in rows if o.delivery_id in notes]
    ann = [n for n in ann if n.note or n.exclude_from_kpi or n.miss_reason
           or (n.result_type and n.result_type != "정상")]
    return {
        "annotated_n": len(ann),
        "noted_n": sum(1 for n in ann if n.note),
        "fail_n": sum(1 for n in ann if n.result_type == "실패"),
        "other_n": sum(1 for n in ann if n.result_type == "기타"),
        "err_n": sum(1 for n in ann if n.result_type == "완료(오류)"),
        "miss_n": sum(1 for n in ann if n.miss_reason),
        "excluded_all_n": sum(1 for n in ann if n.exclude_from_kpi),
    }


def compute_metrics(rows: list[RawOrder], notes: dict) -> dict:
    excluded_n = len(rows) - len(keep(rows, notes))
    completed_all = len(rows)      # 집계제외까지 포함한 완료 건수(표의 "완료" 열). KPI 모수는 아니다.
    annot = annotation_counts(rows, notes)
    rows = keep(rows, notes)
    robots_all = [o for o in rows if o.delivery_type == ROBOT]
    general = [o for o in rows if o.delivery_type != ROBOT]
    loadshop_general = [o for o in general if o.store_type == "일반(로드샵)"]
    b2b_general = [o for o in general if o.store_type == "B2B"]
    loadshop_robot = [o for o in robots_all if o.store_type == "일반(로드샵)"]
    b2b_robot = [o for o in robots_all if o.store_type == "B2B"]
    ls_base = len(loadshop_general) + len(loadshop_robot)
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

    def _stack(sec):
        """스택바용 분 환산. 수 초 역전(음수)은 0 으로 눌러 막대가 뒤로 깎이지 않게 한다."""
        m = _min(sec)
        return None if m is None else max(m, 0.0)

    seg_robot = {
        "①접수→배차": _min(_mean([o.s_order_dispatch for o in robots])),
        "②배차→픽업완료": _min(_mean([o.s_dispatch_pickup for o in robots])),
        "③픽업완료→적재함닫힘": _min(_mean([o.s_pickup_dockclose for o in robots])),
        "④적재함닫힘→로봇배송완료": _min(_mean([o.s_dockclose_robotfinish for o in robots])),
        "⑤배달완료까지": _stack(_mean([_tail_sec(o) for o in robots])),
    }
    seg_general = {
        "①접수→배차": _min(_mean([o.s_order_dispatch for o in general])),
        "②배차→픽업완료": _min(_mean([o.s_dispatch_pickup for o in general])),
        "③픽업완료→적재함닫힘": None,
        "④적재함닫힘→로봇배송완료": None,
        "⑤배달완료까지": _stack(_mean([_tail_sec(o) for o in general])),
    }

    def seg_row(lst, robot_only=False, mixed=False):
        avg = lambda f: _min(_mean([f(o) for o in lst]))
        tail_sec = None if mixed else _mean([_tail_sec(o) for o in lst])
        return {
            "n": len(lst),
            "order_dispatch": avg(lambda o: o.s_order_dispatch),
            "dispatch_pickup": avg(lambda o: o.s_dispatch_pickup),
            "pickup_dock": avg(lambda o: o.s_pickup_dockclose) if robot_only else None,
            "dock_finish": avg(lambda o: o.s_dockclose_robotfinish) if robot_only else None,
            # ⑤는 로봇/일반의 기준 시점이 달라(로봇배송완료 vs 픽업완료) 섞인 모수에서는 내지 않는다
            "tail": _min(tail_sec),
            "tail_instant": robot_only and _is_instant(tail_sec),
            "total": avg(lambda o: o.s_order_finish),
            "pickup_finish": avg(_pickup_to_finish),
        }
    seg_cmp = {"all": seg_row(timed, mixed=True), "general": seg_row(general),
               "robot": seg_row(robots, True)}

    return {
        "seg_cmp": seg_cmp,
        "completed": len(rows),
        "completed_all": completed_all,
        "robot_done": len(robots_all),
        "general_done": len(general),
        "loadshop_done": len(loadshop_general),
        "b2b_done": len(b2b_general),
        "loadshop_robot_done": len(loadshop_robot),
        "b2b_robot_done": len(b2b_robot),
        "usage_loadshop_pct": round(len(loadshop_robot) / ls_base * 100, 1) if ls_base else None,
        "usage_pct": round(len(robots_all) / len(rows) * 100, 1) if rows else None,
        "avg_total_min": _min(avg_total),
        "median_total_min": _min(statistics.median(clean)) if clean else None,
        "avg_pickup_min": _min(avg_pick),
        "avg_total_general_min": _min(_mean([o.s_order_finish for o in general])),
        "avg_total_robot_min": _min(_mean([o.s_order_finish for o in robots])),
        "avg_pickup_robot_min": _min(_mean([_pickup_to_finish(o) for o in robots])),
        "avg_dockclose_robot_min": _min(_mean([o.s_dockclose_robotfinish for o in robots])),
        "robot_total_n": sum(1 for o in robots if o.s_order_finish is not None),
        "robot_pickup_n": sum(1 for o in robots if _pickup_to_finish(o) is not None),
        "robot_dock_n": sum(1 for o in robots if o.s_dockclose_robotfinish is not None),
        "delta_total_min": round(_min(avg_total) - base["total_min"], 1) if avg_total is not None else None,
        "delta_pickup_min": round(_min(avg_pick) - base["pickup_min"], 1) if avg_pick is not None else None,
        "timely_n": timely_n, "timely_base": len(elig),
        "timely_pct": round(timely_n / len(elig) * 100, 1) if elig else None,
        "success_n": succ_n, "success_base": len(succ_base),
        "success_pct": round(succ_n / len(succ_base) * 100, 1) if succ_base else None,
        "excluded_n": excluded_n,
        "time_n": sum(1 for v in total_secs if v is not None),
        "time_excluded_n": len(robots_all) - len(robots),
        **annot, **consent_stats(rows),
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


def period_series(rows, notes, s: date, e: date, unit: str) -> dict:
    """선택 기간 안의 추이 점. unit='day'면 일별, 'week'면 주별(월~일, 기간 경계로 자름). 지표는 표와 같은 compute_metrics."""
    tbl = daily_table(rows, notes, s, e) if unit == "day" else weekly_table(rows, notes, s, e)
    out = {"unit": unit, "labels": [], "robot_min": [], "general_min": [], "usage_all_pct": [],
           "usage_loadshop_pct": [], "n": []}
    for r in reversed(tbl):                     # 표는 최신이 위 → 차트는 과거→현재
        m = r["m"]
        out["labels"].append(f"{r['date']:%m/%d}" if unit == "day" else f"{r['start']:%m/%d}~{r['end']:%m/%d}")
        out["robot_min"].append(m["avg_total_robot_min"])
        out["general_min"].append(m["avg_total_general_min"])
        out["usage_all_pct"].append(m["usage_pct"])
        out["usage_loadshop_pct"].append(m["usage_loadshop_pct"])
        out["n"].append(m["completed"])
    return out


def weekly_series(rows, notes, de: date, weeks: int = 12, anchor: date | None = None) -> dict:
    """주별 추이. anchor(선택한 주의 날짜)가 있으면 그 주가 마지막 점이 되고, 없으면 데이터 마지막 주."""
    last = _monday(anchor or de)
    starts = [last - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    out = {"labels": [], "robot_min": [], "general_min": [], "usage_pct": [], "usage_all_pct": [],
           "usage_loadshop_pct": [], "n": [], "selected": len(starts) - 1}
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
        out["usage_all_pct"].append(round(len(rob_all) / len(wr) * 100, 1) if wr else None)   # 전체_로봇 이용률
        ls = [o for o in wr if o.store_type == LOADSHOP]
        ls_rob = [o for o in ls if o.delivery_type == ROBOT]
        out["usage_loadshop_pct"].append(round(len(ls_rob) / len(ls) * 100, 1) if ls else None)  # 로드샵_로봇 이용률
        out["n"].append(len(wr))
    return out


HEAT_CATS = [
    ("all", "전체 완료", lambda o: True),
    ("ls_rider", "로드샵_라이더", lambda o: o.store_type == LOADSHOP and o.delivery_type != ROBOT),
    ("b2b_rider", "B2B_라이더", lambda o: o.store_type == "B2B" and o.delivery_type != ROBOT),
    ("ls_robot", "로드샵_로봇", lambda o: o.store_type == LOADSHOP and o.delivery_type == ROBOT),
    ("b2b_robot", "B2B_로봇", lambda o: o.store_type == "B2B" and o.delivery_type == ROBOT),
]


def heatmap(rows, s: date, e: date) -> dict:
    """시간대별 완료 건수. 구분(전체·상점구분×배송유형)별 셀을 함께 만든다. 시간 행 범위는 전체 기준으로 고정해
    버튼으로 구분을 바꿔도 행이 움직이지 않는다."""
    days = list(_days(s, e)) if e >= s else []
    hours = sorted({o.ord_dt.hour for o in rows})
    if hours:
        hours = list(range(min(hours), max(hours) + 1))
    cats = []
    for key, label, pred in HEAT_CATS:
        cnt = {}
        for o in rows:
            if pred(o):
                k = (o.ord_dt.date(), o.ord_dt.hour)
                cnt[k] = cnt.get(k, 0) + 1
        cats.append({"key": key, "label": label, "total": sum(cnt.values()),
                     "cells": [[cnt.get((d, h), 0) for d in days] for h in hours],
                     "max": max(cnt.values()) if cnt else 0})
    return {"days": [f"{d:%m/%d}" for d in days], "hours": hours, "cats": cats}


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


def weekly_table(rows, notes, s: date, e: date) -> list[dict]:
    """주간 누적 리스트 (월~일, 최신 주가 위). 기간 밖 날짜는 잘라낸다. 지표 정의는 카드와 동일."""
    if e < s:
        return []
    out = []
    ws = _monday(s)
    while ws <= e:
        a, b = max(ws, s), min(ws + timedelta(days=6), e)
        wr = [o for o in rows if a <= o.ord_dt.date() <= b]
        out.append({"start": a, "end": b, "m": compute_metrics(wr, notes)})
        ws += timedelta(days=7)
    return out[::-1]


def dashboard(session, p: Period) -> dict:
    notes = load_notes(session)
    rows = scoped_done(session, p.start, p.eff_end)
    m = compute_metrics(rows, notes)            # 내부에서 제외 건 반영
    # 특이사항 카드는 목록과 맞추기 위해 미완료 로봇 건도 포함 (다른 지표는 완료 건 기준 그대로)
    unf = unfinished_robots(session, p.start, p.eff_end)
    m.update(annotation_counts(rows + unf, notes))
    m["unfinished_ann_n"] = annotation_counts(unf, notes)["annotated_n"]
    # 일평균 = 건수 ÷ 경과일수(D-1 까지). 진행 중인 주·월도 지난 날짜만큼으로 나눈다.
    n_days = p.elapsed_days
    m["avg_days"] = n_days
    m["avg_completed"] = round(m["completed_all"] / n_days, 1) if n_days else None
    m["avg_robot"] = round(m["robot_done"] / n_days, 1) if n_days else None

    de = data_end(date.today())
    # 트렌드 구간: 일 단위는 최근 14일, 그 외는 선택 기간
    if p.grain == "day":
        ts, te = p.start - timedelta(days=13), p.eff_end
    else:
        ts, te = p.start, p.eff_end
    trend_all = scoped_done(session, ts, te)
    trend_rows = keep(trend_all, notes)
    # 추이 차트는 선택한 기간만: 주 단위는 그 주의 일별, 월·직접 선택은 기간 안의 주별(경계는 기간으로 자름)
    period_pts = period_series(trend_all, notes, ts, te, "day" if p.grain == "week" else "week")
    return {
        "metrics": m,
        "daily": daily_series(trend_rows, ts, te),
        "weekly": period_pts,
        "heat": heatmap(trend_rows, ts, te),
        "day_rows": daily_table(trend_all, notes, ts, te),
        "week_rows": weekly_table(trend_all, notes, ts, te) if p.grain in ("month", "custom") else [],
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
        out = [r for r in out if r["note"] or r["exclude"] or r["result_type"] not in ("", "정상")]
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


# ---------------------------------------------------------------- 로봇 복귀·가용률 (뉴빌리티 수기 기록)
def robot_availability(session, p: Period) -> dict:
    """배송완료 후 로봇이 도킹존으로 돌아오기까지. Redash 는 로봇배송완료에서 끝나서 이 구간이 없다.

    출처가 수기 기록(분 단위)이라 KPI 원본이 아니라 운영 참고값이다. 중앙값을 주로 쓴다 —
    복귀는 엘리베이터·경로 혼잡으로 가끔 크게 튀어 평균이 실태를 과장한다.
    """
    trips = list(session.scalars(
        select(RobotTrip).where(RobotTrip.ord_date >= p.start, RobotTrip.ord_date <= p.eff_end,
                                RobotTrip.status == "완료")))
    done = [t for t in trips if t.s_cycle is not None]
    if not done:
        return {"n": 0, "uploaded_at": _last_upload(session)}

    cyc = [t.s_cycle for t in done]
    ret = [t.s_return for t in done if t.s_return is not None]
    wait = [t.s_finish_return_start for t in done if t.s_finish_return_start is not None]
    after = [t.s_finish_return_start + t.s_return for t in done
             if t.s_finish_return_start is not None and t.s_return is not None]
    cyc_med = _med(cyc)

    by_robot = []
    for rid in sorted({t.robot_id for t in done if t.robot_id}):
        g = [t for t in done if t.robot_id == rid]
        by_robot.append({
            "robot_id": rid, "n": len(g),
            "hand": _min(_med([t.s_dockclose_finish for t in g if t.s_dockclose_finish is not None])),
            "wait": _min(_med([t.s_finish_return_start for t in g if t.s_finish_return_start is not None])),
            "ret": _min(_med([t.s_return for t in g if t.s_return is not None])),
            "cycle": _min(_med([t.s_cycle for t in g])),
        })

    return {
        "n": len(done),
        "robots": len({t.robot_id for t in done if t.robot_id}),
        "days": len({t.ord_date for t in done}),
        "cycle_min": _min(cyc_med),
        "cycle_max_min": _min(max(cyc)),
        "wait_min": _min(_med(wait)),
        "return_min": _min(_med(ret)),
        "after_min": _min(_med(after)),
        "after_max_min": _min(max(after)) if after else None,
        # 1회전 중 배송완료 이후(고객 체감과 무관하게 로봇만 묶여 있는) 비중
        "after_pct": (round(_med(after) / cyc_med * 100, 1) if after and cyc_med else None),
        # 이론 처리량: 1회전 점유시간만으로 환산한 상한. 대기·충전·운영시간은 빼지 않았다.
        "per_hour": (round(3600 / cyc_med, 1) if cyc_med else None),
        "by_robot": by_robot,
        "uploaded_at": _last_upload(session),
    }


def _last_upload(session):
    return session.scalar(select(func.max(RobotTrip.uploaded_at)))


# ---------------------------------------------------------------- 전체 주문 (로봇 누락 파악)
MISS_REASONS = ["시스템 오류(로봇 매칭 실패)", "테스트 주문", "로봇 운영 외(점검·미운영)", "상점 미동의", "기타"]
PER_PAGE = 100

# 구간 분해 (키, 헤더, 설명). ①②⑤는 모든 건, ③④는 로봇 건에만 값이 있다.
# ⑤는 Redash 집계 컬럼이 없어 타임스탬프에서 파생한다(_tail_sec).
SEG_COLS = [
    ("s1", "①배차", "접수→라이더 배차"),
    ("s2", "②픽업", "라이더 배차→픽업완료 (상점 조리 대기 포함)"),
    ("s3", "③적재", "픽업완료→도킹존 적재함닫힘 (로봇 건만)"),
    ("s4", "④로봇", "적재함닫힘→로봇배송완료 (로봇 건만). 7분 초과 시 ▲"),
    ("s5", "⑤완료", "배달완료까지 남은 구간. 로봇 건은 로봇배송완료 이후(완료 처리), "
                   "일반 건은 픽업완료 이후(라이더가 고객에게 전달)"),
]
SEG_KEYS = [k for k, _, _ in SEG_COLS]
SEG_NAMES = {k: lab for k, lab, _ in SEG_COLS}

# 구간 셀 강조(노란색) 기준. "이 건은 이 구간이 비정상"을 집어내는 용도다.
# 행 안에서 제일 큰 칸을 칠하면 한 구간이 대부분을 차지할 때 열 전체가 칠해져 정보가 되지 않는다.
SEG_FLAG_RATIO = 2.0        # 같은 구간·같은 배송 유형의 기간 중앙값 대비 몇 배 이상
SEG_FLAG_MIN_MIN = 5.0      # 절대값 하한(분). ①배차처럼 평균이 짧은 구간에서 1~2분이 '2배'로 칠해지는 것을 막는다
SEG_FLAG_MIN_N = 5          # 표본이 이보다 적으면 평균이 불안정해 칠하지 않는다


# Redash 가 준 구간 초와, 같이 저장한 타임스탬프의 차이가 이보다 벌어지면 둘 중 하나가 틀린 것이다.
# 초 단위 반올림·수집 시점 차이를 감안해 1분을 허용한다.
SEG_MISMATCH_SEC = 60


def _seg_bounds(o: RawOrder) -> dict:
    """구간별 (시작시각, 끝시각, Redash가 준 초). 화면에서 실제 시각을 확인할 수 있게 한다."""
    tail_from = o.robot_finish_dt if o.delivery_type == ROBOT else o.pickup_dt
    return {
        "s1": (o.ord_dt, o.dispatch_dt, o.s_order_dispatch),
        "s2": (o.dispatch_dt, o.pickup_dt, o.s_dispatch_pickup),
        "s3": (o.pickup_dt, o.dock_close_dt, o.s_pickup_dockclose),
        "s4": (o.dock_close_dt, o.robot_finish_dt, o.s_dockclose_robotfinish),
        "s5": (tail_from, o.finish_dt, _tail_sec(o)),
    }


def _seg_times_and_gaps(o: RawOrder) -> tuple[dict, list[str]]:
    """구간별 '시작→끝' 표기와, Redash 값이 타임스탬프와 어긋나는 구간 목록."""
    times, bad = {}, []
    for k, (a, b, sec) in _seg_bounds(o).items():
        times[k] = ((a.strftime("%H:%M:%S") if a else "?") + " → "
                    + (b.strftime("%H:%M:%S") if b else "?"))
        if a is not None and b is not None and sec is not None:
            if abs((b - a).total_seconds() - sec) > SEG_MISMATCH_SEC:
                bad.append(k)
    return times, bad


def _mark_outliers(rows: list[dict]) -> dict:
    """각 행에 hot(강조할 구간 키 목록)을 달고, 기준이 된 구간별 중앙값을 돌려준다.

    기준을 평균이 아니라 중앙값으로 두는 이유: 긴 건 몇 개가 평균을 끌어올려
    자기 자신을 기준 아래로 숨긴다(평균 20분이면 34분짜리가 2배에 못 미친다).
    중앙값은 그 영향을 받지 않아 "대부분의 건 대비 몇 배"가 그대로 나온다.

    기준은 배송 유형(로봇/일반)별로 따로 낸다 — 특히 ⑤는 로봇이 '완료 처리',
    일반이 '라이더 전달'이라 섞으면 기준이 엉킨다. 취소 건과 집계 제외 건은
    구간이 잘려 있거나 실적이 아니므로 기준에서 뺀다.
    """
    base = {}
    for is_robot in (True, False):
        pool = [r for r in rows if r["is_robot"] == is_robot
                and r["delivery_status"] == DONE and not r["exclude"]]
        for k in SEG_KEYS:
            vals = [r["segs"][k] for r in pool if r["segs"][k] is not None]
            base[(is_robot, k)] = (_med(vals), len(vals))
    for r in rows:
        hot = []
        for k in SEG_KEYS:
            v = r["segs"][k]
            mid, n = base[(r["is_robot"], k)]
            if v is None or mid is None or n < SEG_FLAG_MIN_N:
                continue
            # 하한을 함께 걸어 둘 다 만족해야 칠한다. 로봇 ⑤처럼 중앙값이 0 인 구간
            # (구조상 '동시')에서도 하한만 넘으면 잡히도록 max 로 묶는다.
            if v >= max(mid * SEG_FLAG_RATIO, SEG_FLAG_MIN_MIN):
                hot.append(k)
        r["hot"] = hot
    # 화면 각주용: 로봇/일반 각각의 구간 중앙값 (표본 수 포함)
    return {("robot" if ir else "general"): {k: base[(ir, k)] for k in SEG_KEYS}
            for ir in (True, False)}


def _sort_rows(rows, sort: str | None, desc: bool):
    """시간 열 정렬. 값 없는 건(None)은 방향과 무관하게 항상 뒤로 보낸다."""
    if sort != "total" and sort not in SEG_KEYS:
        return rows                                   # 기본 정렬(접수 역순) 유지
    val = (lambda r: r["total_min"]) if sort == "total" else (lambda r: r["segs"][sort])
    sign = -1 if desc else 1
    return sorted(rows, key=lambda r: (val(r) is None, sign * (val(r) or 0)))


def order_row_view(o: RawOrder, n: RobotOrderNote | None) -> dict:
    segs = {"s1": _min(o.s_order_dispatch), "s2": _min(o.s_dispatch_pickup),
            "s3": _min(o.s_pickup_dockclose), "s4": _min(o.s_dockclose_robotfinish),
            "s5": _min(_tail_sec(o))}
    longest = max((k for k in SEG_KEYS if segs[k] is not None), key=lambda k: segs[k], default=None)
    tail_instant = o.delivery_type == ROBOT and _is_instant(_tail_sec(o))
    seg_times, seg_bad = _seg_times_and_gaps(o)
    return {
        "delivery_id": o.delivery_id, "order_id": o.order_id, "ord_dt": o.ord_dt,
        "delivery_type": o.delivery_type, "is_robot": o.delivery_type == ROBOT,
        "robot_name": o.robot_name, "order_source": o.order_source, "store_name": o.store_name,
        "store_type": o.store_type, "consent": o.store_robot_consent or "", "delivery_status": o.delivery_status,
        "total_min": _min(o.s_order_finish), "dispatch_count": o.dispatch_count,
        "hand_min": _min(o.s_dockclose_robotfinish),
        "late": (o.s_dockclose_robotfinish is not None and o.s_dockclose_robotfinish > CFG["timely_threshold_sec"]),
        "segs": segs, "longest": longest, "tail_instant": tail_instant, "hot": [],
        "seg_times": seg_times, "seg_bad": seg_bad,
        "in_scope": o.store_type in CFG["store_types"],
        "miss_reason": (n.miss_reason if n and n.miss_reason else ""),
        "note": (n.note if n and n.note else ""),
        "exclude": bool(n and n.exclude_from_kpi),
        "updated_at": n.updated_at if n else None, "updated_by": n.updated_by if n else None,
    }


def _period_rows(session, p: Period) -> tuple[list[dict], dict]:
    """기간 전체 행 + 구간 평균. 강조 기준은 필터와 무관하게 기간 전체로 고정한다."""
    lo, hi = _bounds(p.start, p.eff_end)
    stmt = (select(RawOrder).where(RawOrder.building == CFG["building"],
                                   RawOrder.ord_dt >= lo, RawOrder.ord_dt < hi)
            .order_by(RawOrder.ord_dt.desc()))
    notes = load_notes(session)
    rows = [order_row_view(o, notes.get(o.delivery_id)) for o in session.scalars(stmt)]
    return rows, _mark_outliers(rows)


def order_row_marked(session, p: Period, delivery_id: str) -> dict | None:
    """단건 재렌더용. 강조는 기간 평균이 있어야 정해지므로 같은 기간을 다시 깐다."""
    rows, _ = _period_rows(session, p)
    return next((r for r in rows if r["delivery_id"] == delivery_id), None)


def order_list(session, p: Period, flt: str | None = None, st: str | None = None,
               q: str | None = None, page: int = 1,
               sort: str | None = None, desc: bool = True) -> dict:
    if p.eff_end < p.start:
        return {"rows": [], "total": 0, "pages": 1, "page": 1, "counts": {}, "seg_avg": {}}
    rows, seg_avg = _period_rows(session, p)
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
    rows = _sort_rows(rows, sort, desc)
    total = len(rows)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(max(page, 1), pages)
    return {"rows": rows[(page - 1) * PER_PAGE: page * PER_PAGE], "total": total,
            "pages": pages, "page": page, "counts": counts, "seg_avg": seg_avg}
