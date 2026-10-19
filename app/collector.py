"""Redash 수집 → raw_orders upsert (최근 N일 롤링 리플레이)."""
import json
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import func, select

from .config import CFG
from .db import (ALL_COLS, DT_COLS, REQUIRED_COLS, SEC_COLS, STR_COLS, CollectLog, RawOrder,
                 get_session_factory)


class CollectError(Exception):
    pass


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    return v


def _id_str(v):
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def validate_columns(columns) -> None:
    missing = [c for c in REQUIRED_COLS if c not in columns]
    if missing:
        raise CollectError(f"필수 컬럼 누락: {missing}")


def _gap_note(columns) -> str:
    """누락 컬럼을 로그 메시지에 붙일 짧은 문구. 메시지는 화면에서 잘리므로 앞에 핵심만 둔다."""
    gap = missing_columns(columns)
    if not gap:
        return ""
    head = ", ".join(gap[:4]) + (f" 외 {len(gap) - 4}개" if len(gap) > 4 else "")
    return f" · ⚠ 쿼리에 없는 컬럼 {len(gap)}개({head}) — 쿼리 저장 여부와 REDASH_QUERY_ID 확인"


def missing_columns(columns) -> list[str]:
    """필수는 아니지만 우리가 읽는 컬럼 중 쿼리에 없는 것. 있으면 해당 화면이 통째로 비어 보인다."""
    have = set(columns)
    return [c for c in ALL_COLS if c not in have]


def df_to_orders(df: pd.DataFrame) -> list[RawOrder]:
    validate_columns(df.columns)
    now = datetime.now()
    out = []
    for rec in df.to_dict("records"):
        o = RawOrder(collected_at=now)
        for k, a in STR_COLS.items():
            setattr(o, a, _id_str(rec.get(k)))
        for k, a in DT_COLS.items():
            v = _clean(rec.get(k))
            setattr(o, a, pd.to_datetime(v).to_pydatetime() if v is not None else None)
        for k, a in SEC_COLS.items():
            v = _clean(rec.get(k))
            setattr(o, a, float(v) if v is not None else None)
        dc = _clean(rec.get("배차횟수"))
        o.dispatch_count = int(dc) if dc is not None else None
        o.raw_json = json.dumps({k: (None if _clean(v) is None else str(v)) for k, v in rec.items()},
                                ensure_ascii=False)
        if o.delivery_id:
            out.append(o)
    return out


CONSENT_COL, STORE_COL = "상점로봇동의상태", "상점ID"


def consent_map(df: pd.DataFrame) -> dict[str, tuple[str | None, str | None]]:
    """배달ID → (상점로봇동의상태, 상점ID). 쿼리에 동의 컬럼이 없으면 빈 dict."""
    if CONSENT_COL not in df.columns:
        return {}
    out = {}
    for rec in df.to_dict("records"):
        did = _id_str(rec.get("배달ID"))
        if did:
            out[did] = (_id_str(rec.get(CONSENT_COL)), _id_str(rec.get(STORE_COL)))
    return out


def backfill_consent(start: date, end: date, dry_run: bool = False, fetch=None) -> dict:
    """이미 쌓인 raw_orders 행에 상점로봇동의상태·상점ID 만 채운다(월 단위 조회).

    전체 재수집과 달리 나머지 컬럼은 건드리지 않는다 — 쿼리가 ver 바뀌며 컬럼이
    없어지거나 의미가 달라져도 이미 산출된 시간·상태 값이 휩쓸리지 않게 하려는 것이다.
    DB 에 없는 배달ID 는 새로 만들지 않는다(주문 적재는 수집 배치 책임).
    collect_log 에는 남기지 않는다 — success 행을 남기면 kpi.freshness 의
    '마지막 수집 시각' 이 실제 수집과 무관하게 갱신돼 데이터가 최신인 것처럼 보인다.
    """
    fetch = fetch or fetch_redash
    SL = get_session_factory()
    st = {"months": 0, "fetched": 0, "matched": 0, "updated": 0, "not_in_db": 0,
          "no_consent_col": [], "failed": []}
    for a, b in month_chunks(start, end):
        st["months"] += 1
        try:
            frame = fetch(a, b)
        except Exception as e:     # noqa: BLE001 - 한 달 실패로 전체를 멈추지 않는다
            st["failed"].append(f"{a:%Y-%m}: {e}")
            continue
        if frame is None or not len(frame):
            continue
        st["fetched"] += len(frame)
        if CONSENT_COL not in frame.columns:
            st["no_consent_col"].append(f"{a:%Y-%m}")
            continue
        pairs = consent_map(frame)
        ids = list(pairs)
        with SL() as s:
            for i in range(0, len(ids), 500):
                batch = ids[i:i + 500]
                rows = list(s.scalars(select(RawOrder)
                                      .where(RawOrder.delivery_id.in_(batch))))
                st["matched"] += len(rows)
                st["not_in_db"] += len(batch) - len(rows)
                for o in rows:
                    consent, store_id = pairs[o.delivery_id]
                    changed = False
                    if consent and o.store_robot_consent != consent:
                        o.store_robot_consent = consent
                        changed = True
                    # 상점ID 도 같이 채운다 — 동의율은 상점 단위 집계라 상점ID 가 없으면
                    # 값이 있어도 '상점 수' 모수에서 빠진다(kpi.consent_stats).
                    if store_id and o.store_id != store_id:
                        o.store_id = store_id
                        changed = True
                    if changed:
                        st["updated"] += 1
            if dry_run:
                s.rollback()
            else:
                s.commit()
    return st


def last_response_columns(session) -> dict:
    """가장 최근 수집 행의 raw_json 으로 Redash 가 실제로 보낸 컬럼을 되짚는다.

    "쿼리는 고쳤는데 화면이 비어 있다" 는 대개 수집이 다른 쿼리를 실행했거나 저장되지 않은
    경우다. 수집은 필수 7개만 검사하고 나머지는 조용히 NULL 로 넣으므로 로그만 봐서는
    모른다. 관리자 화면에서 한눈에 보려고 쓴다(scripts/check_columns.py 와 같은 판정).
    """
    o = session.scalars(select(RawOrder).where(RawOrder.raw_json.is_not(None))
                        .order_by(RawOrder.collected_at.desc()).limit(1)).first()
    if o is None:
        return {"n": 0, "missing": [], "collected_at": None}
    cols = list(json.loads(o.raw_json))
    return {"n": len(cols), "missing": [c for c in ALL_COLS if c not in cols],
            "collected_at": o.collected_at.strftime("%m-%d %H:%M") if o.collected_at else None}


def orders_date_range(session) -> tuple[date | None, date | None]:
    """이미 쌓인 주문의 접수일 범위 = '전체 데이터'. 백필에서 빈 달을 조회하지 않으려고 쓴다."""
    lo, hi = session.execute(select(func.min(RawOrder.ord_dt), func.max(RawOrder.ord_dt))).one()
    return (lo.date() if lo else None, hi.date() if hi else None)


def consent_fill_by_month(session) -> list[tuple[str, int, int]]:
    """(년-월, 전체 건수, 동의상태 채워진 건수). 백필 전후 비교용.

    월 묶기는 파이썬에서 한다 — SQLite 와 Postgres 의 날짜 포맷 함수가 달라
    SQL 로 하면 둘 중 하나에서만 돈다. 읽는 컬럼은 두 개뿐이다.
    """
    out = {}
    for ord_dt, consent in session.execute(select(RawOrder.ord_dt, RawOrder.store_robot_consent)):
        key = f"{ord_dt:%Y-%m}" if ord_dt else "(접수일시 없음)"
        tot, got = out.get(key, (0, 0))
        out[key] = (tot + 1, got + (1 if consent else 0))
    return [(k, v[0], v[1]) for k, v in sorted(out.items())]


def upsert_orders(orders: list[RawOrder], since: date | None = None) -> int:
    """since가 있으면 접수일 >= since 인 건만 반영(롤링 리플레이)."""
    SL = get_session_factory()
    n = 0
    with SL() as s:
        for o in orders:
            if since and (o.ord_dt is None or o.ord_dt.date() < since):
                continue
            s.merge(o)   # PK=delivery_id, 사용자 메모 테이블은 건드리지 않음
            n += 1
        s.commit()
    return n


def fetch_redash(start: date, end: date, query_id: int | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: int = 180) -> pd.DataFrame:
    base_url = (base_url or os.getenv("REDASH_URL", "")).rstrip("/")
    api_key = api_key or os.getenv("REDASH_API_KEY")
    query_id = query_id or os.getenv("REDASH_QUERY_ID")
    if not (base_url and api_key and query_id):
        raise CollectError("REDASH_URL / REDASH_API_KEY / REDASH_QUERY_ID 가 .env에 필요합니다")
    headers = {"Authorization": f"Key {api_key}"}
    r = requests.post(f"{base_url}/api/queries/{query_id}/results",
                      json={"max_age": 0, "parameters": {
                          "기간": {"start": start.isoformat(), "end": end.isoformat()},
                          "건물명": CFG["building"]}}, headers=headers, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    deadline = time.time() + timeout
    while "job" in body:     # 실행 job 폴링
        job = body["job"]
        if job.get("status") == 3:
            rid = job["query_result_id"]
            r = requests.get(f"{base_url}/api/query_results/{rid}", headers=headers, timeout=timeout)
            r.raise_for_status()
            body = r.json()
            break
        if job.get("status") in (4, 5) or time.time() > deadline:
            raise CollectError(f"Redash job 실패/타임아웃: {job.get('error')}")
        time.sleep(2)
        r = requests.get(f"{base_url}/api/jobs/{job['id']}", headers=headers, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    rows = body["query_result"]["data"]["rows"]
    return pd.DataFrame(rows)


def read_xlsx(path: str) -> pd.DataFrame:
    return pd.read_excel(path)


def log(status: str, rows: int, message: str = "") -> None:
    SL = get_session_factory()
    with SL() as s:
        s.add(CollectLog(ts=datetime.now(), status=status, rows=rows, message=message))
        s.commit()


def month_chunks(start: date, end: date):
    """Redash 스캔 용량 한도를 피하기 위해 월 단위로 쪼갠다."""
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
        yield max(d, start), min(nxt - timedelta(days=1), end)
        d = nxt


def backfill(start: date, end: date) -> int:
    """월 단위로 조회·upsert. 월별로 저장해 중간에 실패해도 받은 달은 남는다."""
    total, failed = 0, []
    for a, b in month_chunks(start, end):
        try:
            frame = fetch_redash(a, b)
            if len(frame):
                total += upsert_orders(df_to_orders(frame))
            log("success", len(frame),
                f"백필 {a}~{b}{_gap_note(frame.columns) if len(frame) else ''}")
        except Exception as e:     # noqa: BLE001
            failed.append(f"{a:%Y-%m}")
            log("fail", 0, f"백필 {a}~{b}: {e}")
    if failed and total == 0:
        raise CollectError(f"백필 전체 실패: {failed}")
    if failed:
        log("fail", total, f"일부 월 실패: {failed}")
    return total


def run_collect(df: pd.DataFrame | None = None, full: bool = False,
                retries: int | None = None, interval: int | None = None) -> int:
    """df가 없으면 Redash에서 수집. 실패 시 재시도 후 로그만 남기고 마지막 성공 데이터 유지."""
    cfg = CFG["collect"]
    retries = cfg["retries"] if retries is None else retries
    interval = cfg["retry_interval_sec"] if interval is None else interval
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            today = date.today()
            since = None if full else today - timedelta(days=cfg["rolling_days"])
            if df is None and full:
                return backfill(date.fromisoformat(str(cfg["full_start"])), today)
            if df is not None:
                frame = df
            else:
                frame = fetch_redash(since, today)
            if frame is None or len(frame) == 0:
                raise CollectError("조회 결과 0건")
            orders = df_to_orders(frame)
            n = upsert_orders(orders, since)
            # 누락 컬럼은 성공 로그에 함께 남긴다. 별도 fail 행으로 남기면 kpi.freshness 의
            # last_failed 가 켜져 "수집 실패" 로 잘못 보인다.
            log("success", n, f"{'전체' if full else '최근 %d일' % cfg['rolling_days']} upsert"
                              f"{_gap_note(frame.columns)}")
            return n
        except Exception as e:     # noqa: BLE001 - 배치는 모든 실패를 로그로 남긴다
            last_err = e
            log("fail", 0, f"시도 {attempt}/{retries}: {e}")
            if attempt < retries and df is None:
                time.sleep(interval)
            else:
                break
    raise CollectError(str(last_err))
