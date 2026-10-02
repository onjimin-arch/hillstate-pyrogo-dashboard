"""Redash 수집 → raw_orders upsert (최근 N일 롤링 리플레이)."""
import json
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from .config import CFG
from .db import (DT_COLS, REQUIRED_COLS, SEC_COLS, STR_COLS, CollectLog, RawOrder,
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
            log("success", len(frame), f"백필 {a}~{b}")
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
            log("success", n, f"{'전체' if full else '최근 %d일' % cfg['rolling_days']} upsert")
            return n
        except Exception as e:     # noqa: BLE001 - 배치는 모든 실패를 로그로 남긴다
            last_err = e
            log("fail", 0, f"시도 {attempt}/{retries}: {e}")
            if attempt < retries and df is None:
                time.sleep(interval)
            else:
                break
    raise CollectError(str(last_err))
