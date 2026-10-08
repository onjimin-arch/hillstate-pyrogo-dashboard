"""Redash 수집 경로 테스트.

여기가 깨지면 데이터가 안 들어온다. 특히 Redash 쿼리의 파라미터 이름·컬럼명이
바뀌면 조용히 전부 실패하므로, 코드가 무엇을 기대하는지 테스트로 고정해 둔다.

네트워크를 타지 않는다. requests 를 가짜 응답으로 바꿔 끼운다.
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import collector  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import CFG  # noqa: E402

# Redash 가 돌려주는 1건. 한글 컬럼명이 db.py 의 매핑 키와 같아야 한다.
ROW = {
    "배달ID": 1234567, "주문ID": "ORD-1", "로봇배송ID": "RBT-1",
    "건물명": CFG["building"], "배송유형": "로봇연계", "상점구분": "일반(로드샵)",
    "로봇매칭여부": "Y", "로봇명": "1호기", "상점명": "가맹점A", "주문처": "채널A",
    "라이더ID": "R-0000", "주문상태": "FINISHED", "배달상태": "DROP_FINISHED", "배차횟수": 1,
    "상점주문접수일시": "2026-10-01 18:00:00", "라이더배차일시": "2026-10-01 18:02:00",
    "라이더픽업완료일시": "2026-10-01 18:10:00", "도킹존_적재함닫힘일시": "2026-10-01 18:15:00",
    "로봇배송완료일시": "2026-10-01 18:20:00", "배달완료일시": "2026-10-01 18:20:00",
    "주문접수_배차_초": 120, "배차_픽업완료_초": 480, "주문접수_픽업완료_초": 600,
    "픽업완료_적재함닫힘_초": 300, "적재함닫힘_로봇배송완료_초": 300, "주문접수_배달완료_초": 1200,
}


class Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


@pytest.fixture()
def redash_env(monkeypatch):
    monkeypatch.setenv("REDASH_URL", "https://redash.example.invalid/")
    monkeypatch.setenv("REDASH_API_KEY", "test-key")
    monkeypatch.setenv("REDASH_QUERY_ID", "42")
    monkeypatch.setattr(collector.time, "sleep", lambda s: None)   # job 폴링 대기 생략


@pytest.fixture()
def calls(monkeypatch):
    """requests.post/get 을 가짜로 바꾸고 호출 내역을 모아준다."""
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        seen.append(("POST", url, headers, json))
        return Resp({"job": {"id": "job-1", "status": 1}})        # 아직 실행 중

    def get(url, headers=None, timeout=None):
        seen.append(("GET", url, headers, None))
        if "/api/jobs/" in url:
            return Resp({"job": {"id": "job-1", "status": 3, "query_result_id": 777}})
        if "/api/query_results/" in url:
            return Resp({"query_result": {"data": {"rows": [ROW]}}})
        raise AssertionError(f"예상치 못한 GET: {url}")

    monkeypatch.setattr(collector.requests, "post", post)
    monkeypatch.setattr(collector.requests, "get", get)
    return seen


def test_fetch_redash_job_polling(redash_env, calls):
    """job 이 바로 안 끝나면 폴링해서 결과를 가져온다."""
    df = collector.fetch_redash(date(2026, 10, 1), date(2026, 10, 1))
    assert len(df) == 1

    urls = [u for _, u, _, _ in calls]
    assert urls[0].endswith("/api/queries/42/results")     # 쿼리 ID 가 URL 에 들어간다
    assert "/api/jobs/job-1" in urls[1]
    assert "/api/query_results/777" in urls[2]


def test_fetch_redash_sends_expected_parameters(redash_env, calls):
    """쿼리 파라미터 이름이 바뀌면 수집이 전부 실패하므로 여기서 고정한다.

    Redash 쿼리에 '기간'(날짜 범위)과 '건물명' 파라미터가 있어야 한다.
    """
    collector.fetch_redash(date(2026, 9, 25), date(2026, 10, 1))

    _, _, headers, body = calls[0]
    assert headers["Authorization"] == "Key test-key"
    assert body["max_age"] == 0                             # 캐시 말고 새로 실행
    assert body["parameters"]["기간"] == {"start": "2026-09-25", "end": "2026-10-01"}
    assert body["parameters"]["건물명"] == CFG["building"]


def test_fetch_redash_requires_env(monkeypatch):
    monkeypatch.delenv("REDASH_URL", raising=False)
    monkeypatch.delenv("REDASH_API_KEY", raising=False)
    monkeypatch.delenv("REDASH_QUERY_ID", raising=False)
    with pytest.raises(collector.CollectError, match="REDASH_URL"):
        collector.fetch_redash(date(2026, 10, 1), date(2026, 10, 1))


def test_fetch_redash_job_failure(redash_env, monkeypatch):
    """job 이 실패 상태(4)로 오면 조용히 넘기지 않고 에러를 낸다."""
    monkeypatch.setattr(collector.requests, "post",
                        lambda *a, **k: Resp({"job": {"id": "j", "status": 4,
                                                      "error": "권한 없음"}}))
    with pytest.raises(collector.CollectError, match="권한 없음"):
        collector.fetch_redash(date(2026, 10, 1), date(2026, 10, 1))


def test_df_to_orders_maps_columns():
    o = collector.df_to_orders(pd.DataFrame([ROW]))[0]
    assert o.delivery_id == "1234567"                       # 숫자로 와도 문자열로 저장
    assert o.building == CFG["building"]
    assert o.robot_matched == "Y"
    assert o.ord_dt == datetime(2026, 10, 1, 18, 0)
    assert o.s_dockclose_robotfinish == 300.0
    assert o.dispatch_count == 1
    assert "가맹점A" in o.raw_json                           # 원본 41컬럼은 raw_json 에 보관


def test_df_to_orders_handles_blanks():
    """빈 값이 0 이나 'nan' 문자열로 들어가면 지표가 틀어진다. None 이어야 한다."""
    row = {**ROW, "로봇배송완료일시": None, "적재함닫힘_로봇배송완료_초": None, "배차횟수": None}
    o = collector.df_to_orders(pd.DataFrame([row]))[0]
    assert o.robot_finish_dt is None
    assert o.s_dockclose_robotfinish is None
    assert o.dispatch_count is None


def test_df_to_orders_skips_rows_without_delivery_id():
    row = {**ROW, "배달ID": None}
    assert collector.df_to_orders(pd.DataFrame([row])) == []


def test_validate_columns_rejects_missing():
    with pytest.raises(collector.CollectError, match="필수 컬럼 누락"):
        collector.validate_columns(["배달ID"])


def test_upsert_orders_since_filters_old_rows(monkeypatch):
    """롤링 수집은 기준일 이전 건을 반영하지 않는다."""
    SL = dbm.make_session_factory("sqlite://")
    monkeypatch.setattr(dbm, "SessionLocal", SL)

    old = {**ROW, "배달ID": 1, "상점주문접수일시": "2026-09-01 12:00:00"}
    new = {**ROW, "배달ID": 2, "상점주문접수일시": "2026-10-01 12:00:00"}
    orders = collector.df_to_orders(pd.DataFrame([old, new]))

    assert collector.upsert_orders(orders, since=date(2026, 9, 25)) == 1


def test_month_chunks_splits_by_month():
    """Redash 스캔 한도를 피하려고 월 단위로 쪼갠다. 연말 경계도 넘어가야 한다."""
    chunks = list(collector.month_chunks(date(2025, 11, 15), date(2026, 1, 10)))
    assert chunks == [
        (date(2025, 11, 15), date(2025, 11, 30)),
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 10)),
    ]


def test_month_chunks_single_day():
    assert list(collector.month_chunks(date(2026, 10, 2), date(2026, 10, 2))) == [
        (date(2026, 10, 2), date(2026, 10, 2))]


def test_df_to_orders_maps_store_consent():
    """Redash 쿼리의 상점ID·상점로봇동의상태가 정식 컬럼으로 들어간다. 쿼리에 없으면 None(수집은 안 깨짐)."""
    o = collector.df_to_orders(pd.DataFrame([{**ROW, "상점ID": 77, "상점로봇동의상태": "미동의"}]))[0]
    assert o.store_id == "77" and o.store_robot_consent == "미동의"
    o = collector.df_to_orders(pd.DataFrame([ROW]))[0]
    assert o.store_id is None and o.store_robot_consent is None


def test_migrate_adds_consent_columns_to_existing_db(tmp_path):
    """컬럼 추가 전 DB(운영 Postgres 와 같은 상황)에서도 시작 시 ALTER 로 보강된다."""
    from sqlalchemy import create_engine, inspect, text
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE raw_orders (delivery_id VARCHAR PRIMARY KEY)"))
        c.execute(text("CREATE TABLE robot_order_notes (delivery_id VARCHAR PRIMARY KEY, miss_reason VARCHAR)"))
        c.execute(text("INSERT INTO raw_orders VALUES ('1')"))
    dbm._migrate(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("raw_orders")}
    assert {"store_id", "store_robot_consent"} <= cols
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM raw_orders")).scalar() == 1   # 기존 행 보존
