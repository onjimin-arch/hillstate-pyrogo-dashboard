"""배치 수집 CLI.

  python scripts/collect.py                       # Redash에서 최근 7일 재수집 (스케줄러용)
  python scripts/collect.py --full                # Redash 전체 결과 적재 (8월 baseline 등 백필)
  python scripts/collect.py --xlsx 파일.xlsx --full  # Redash 없이 엑셀로 적재(개발/검증용)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from app.collector import CollectError, read_xlsx, run_collect  # noqa: E402
from app.config import ROOT  # noqa: E402

load_dotenv(ROOT / ".env")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="롤링 윈도우 없이 전체 적재")
    ap.add_argument("--xlsx", help="Redash 대신 엑셀 파일에서 적재")
    a = ap.parse_args()
    try:
        df = read_xlsx(a.xlsx) if a.xlsx else None
        n = run_collect(df=df, full=a.full)
    except CollectError as e:
        print(f"수집 실패: {e}", file=sys.stderr)
        return 1
    print(f"수집 완료: {n}건 upsert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
