"""수집된 주문의 raw_json 을 열어 Redash 가 실제로 보낸 컬럼을 확인한다.

화면이 통째로 "-" 로 나올 때 쓴다. 수집은 필수 7개 컬럼만 검사하고 나머지는 없어도 조용히
NULL 로 넣기 때문에, "쿼리는 고쳤는데 값이 안 나온다"면 대개 수집된 쿼리가 다른 버전이다.

    DATABASE_URL=... python scripts/check_columns.py
    DATABASE_URL=... python scripts/check_columns.py --col 상점로봇동의상태
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import ALL_COLS, RawOrder, get_session_factory  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="최근 몇 건을 볼지 (기본 200)")
    ap.add_argument("--col", default=None, help="이 컬럼의 값 분포만 본다")
    a = ap.parse_args()

    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL 을 주세요. 운영 DB 를 보려면 운영 값을 넣어야 합니다.")

    SL = get_session_factory()
    with SL() as s:
        rows = list(s.scalars(select(RawOrder).order_by(RawOrder.collected_at.desc(),
                                                        RawOrder.ord_dt.desc()).limit(a.limit)))
    if not rows:
        raise SystemExit("raw_orders 가 비어 있습니다.")

    parsed = [json.loads(r.raw_json) for r in rows if r.raw_json]
    if not parsed:
        raise SystemExit("raw_json 이 비어 있습니다(수집 경로가 아니라 가져오기로 넣은 데이터일 수 있습니다).")

    seen = set()
    for rec in parsed:
        seen |= set(rec)
    print(f"최근 {len(rows)}건 · 수집 시각 {rows[0].collected_at} · 접수 {rows[-1].ord_dt} ~ {rows[0].ord_dt}")
    print(f"Redash 가 보낸 컬럼 {len(seen)}개\n")

    missing = [c for c in ALL_COLS if c not in seen]
    if missing:
        print("[문제] 우리가 읽는데 응답에 없는 컬럼 — 화면에서 전부 비어 보입니다:")
        for c in missing:
            print(f"   - {c}")
        print("\n  → Redash 에서 쿼리를 '저장'했는지, REDASH_QUERY_ID 가 그 쿼리를 가리키는지 확인하세요.")
        print("    (수집은 저장된 쿼리를 실행합니다. 편집창에서만 고친 내용은 반영되지 않습니다.)")
    else:
        print("[정상] 우리가 읽는 컬럼은 모두 응답에 있습니다.")

    if a.col:
        print(f"\n=== {a.col} 값 분포 ===")
        if a.col not in seen:
            print("   응답에 이 컬럼이 없습니다.")
        else:
            c = Counter((rec.get(a.col) if rec.get(a.col) not in ("", None) else "(빈값)")
                        for rec in parsed)
            for v, n in c.most_common():
                print(f"   {v}: {n}건")
        print(f"\n=== DB 에 저장된 store_robot_consent 분포 ===")
        c2 = Counter(r.store_robot_consent or "(NULL)" for r in rows)
        for v, n in c2.most_common():
            print(f"   {v}: {n}건")


if __name__ == "__main__":
    main()
