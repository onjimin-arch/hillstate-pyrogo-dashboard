"""이미 쌓인 주문 전체에 Redash 의 보강 컬럼(collector.FILL_COLS)을 채운다.

지금 채우는 건 `상점로봇동의상태`·`상점ID`·`결제방식` 세 개다. 쿼리에 나중에 추가된 컬럼이라
그 전에 수집된 행은 NULL 이고 대시보드에서 "-" 로 나온다. 전체 재수집(`collect.py --full`)도
값을 채우지만 모든 컬럼을 덮어쓰므로, 쿼리 버전이 바뀐 뒤에는 이미 산출된 시간·상태 값까지
따라 바뀐다. 이 스크립트는 FILL_COLS 만 건드린다.

  DATABASE_URL=... python scripts/backfill_consent.py              # DB 에 있는 주문 전 기간
  DATABASE_URL=... python scripts/backfill_consent.py --dry-run    # 바꾸지 않고 건수만
  DATABASE_URL=... python scripts/backfill_consent.py --since 2026-08-01 --until 2026-10-18

REDASH_URL / REDASH_API_KEY / REDASH_QUERY_ID 가 필요하다(.env 또는 환경변수).
REDASH_QUERY_ID 는 그 컬럼들이 **저장된** 쿼리를 가리켜야 한다 — 편집창에서만 고친 내용은
API 로 실행되지 않는다. 월 단위로 조회하므로 기간이 길면 Redash 스캔량이 그만큼 늘어난다.
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from app.collector import (CollectError, backfill_added_columns,  # noqa: E402
                           consent_fill_by_month, orders_date_range)
from app.db import get_session_factory  # noqa: E402

load_dotenv(ROOT / ".env")


def _report(title: str) -> None:
    SL = get_session_factory()
    with SL() as s:
        rows = consent_fill_by_month(s)
    print(f"\n=== {title} (월별 동의상태 채움) ===")
    tot = got = 0
    for key, n, filled in rows:
        tot, got = tot + n, got + filled
        print(f"   {key}  {filled:>6,} / {n:>6,} 건  ({filled / n * 100:5.1f}%)")
    if tot:
        print(f"   {'합계':<5}  {got:>6,} / {tot:>6,} 건  ({got / tot * 100:5.1f}%)")


def _db_range() -> tuple[date, date]:
    """DB 에 있는 주문의 접수일 범위 = '전체 데이터'. 빈 달을 조회하지 않으려는 것."""
    SL = get_session_factory()
    with SL() as s:
        lo, hi = orders_date_range(s)
    if lo is None:
        raise SystemExit("raw_orders 가 비어 있습니다. 먼저 수집을 돌려주세요.")
    return lo, hi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="시작일 YYYY-MM-DD (기본: DB 의 가장 이른 접수일)")
    ap.add_argument("--until", help="종료일 YYYY-MM-DD (기본: DB 의 가장 늦은 접수일)")
    ap.add_argument("--dry-run", action="store_true", help="DB 를 바꾸지 않고 건수만 센다")
    a = ap.parse_args()

    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL 을 주세요. 운영 데이터를 채우려면 운영 값이어야 합니다.")

    lo, hi = _db_range()
    start = date.fromisoformat(a.since) if a.since else lo
    end = date.fromisoformat(a.until) if a.until else hi
    if start > end:
        raise SystemExit(f"기간이 뒤집혔습니다: {start} ~ {end}")

    _report("백필 전")
    print(f"\nRedash 조회 {start} ~ {end} (월 단위){' · dry-run' if a.dry_run else ''}")
    try:
        st = backfill_added_columns(start, end, dry_run=a.dry_run)
    except CollectError as e:
        print(f"백필 실패: {e}", file=sys.stderr)
        return 1

    print(f"\n조회 {st['months']}개월 · Redash {st['fetched']:,}건 · DB 일치 {st['matched']:,}건 "
          f"· {'바뀔 행' if a.dry_run else '갱신'} {st['updated']:,}건 · DB 에 없는 주문 {st['not_in_db']:,}건")
    for col, months in st["no_col"].items():
        print(f"[문제] 응답에 {col} 컬럼이 없는 달: {', '.join(months)}")
        print("   → Redash 에서 쿼리를 '저장'했는지, REDASH_QUERY_ID 가 그 쿼리인지 확인하세요.")
    for f in st["failed"]:
        print(f"[실패] {f}")

    if not a.dry_run:
        _report("백필 후")
    return 1 if (st["failed"] or st["no_col"]) else 0


if __name__ == "__main__":
    sys.exit(main())
