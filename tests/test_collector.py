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


def test_df_to_orders_maps_payment_method():
    """결제방식이 정식 컬럼으로 들어간다. 쿼리에 없으면 None 이고 수집은 안 깨진다.

    빈 값을 '선결제' 로 메우지 않는다 — 현장결제는 로봇이 수납을 못 해 로봇으로 못 가는
    건이라, 모르는 값을 선결제로 보면 '로봇으로 갈 수 있었다' 는 판정이 뒤집힌다.
    """
    o = collector.df_to_orders(pd.DataFrame([{**ROW, "결제방식": "현장카드결제"}]))[0]
    assert o.payment_method == "현장카드결제"
    assert collector.df_to_orders(pd.DataFrame([ROW]))[0].payment_method is None


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
    assert {"store_id", "store_robot_consent", "payment_method"} <= cols
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM raw_orders")).scalar() == 1   # 기존 행 보존


def test_missing_columns_warns_without_failing_collection(monkeypatch, tmp_path):
    """필수가 아닌 컬럼이 빠져도 수집은 성공하되, 로그에 누락이 남아야 한다.
    경고를 fail 행으로 남기면 freshness 의 last_failed 가 켜져 '수집 실패' 로 잘못 보인다."""
    import pandas as pd
    from app import collector, db as dbm, kpi
    from sqlalchemy import select

    SL = dbm.make_session_factory("sqlite://")
    monkeypatch.setattr(dbm, "SessionLocal", SL)
    monkeypatch.setattr(dbm, "get_session_factory", lambda: SL)
    monkeypatch.setattr(collector, "get_session_factory", lambda: SL)

    # 실제로 일어난 모양: 다른 컬럼은 다 있고 동의 컬럼 하나만 빠진 쿼리
    # (ROW 에는 나중에 추가된 상점ID·상점로봇동의상태가 없으므로 여기서 채워 완전한 행을 만든다)
    # 접수일시는 오늘로 둔다 — upsert_orders 가 최근 rolling_days 밖의 건을 걸러낸다
    today_dt = f"{date.today()} 18:00:00"
    full = {**ROW, "배달ID": "1", "상점ID": "S-1", "상점로봇동의상태": "동의",
            "결제방식": "선결제", "상점주문접수일시": today_dt}
    only_consent_missing = {k: v for k, v in full.items() if k != "상점로봇동의상태"}
    assert collector.missing_columns(only_consent_missing) == ["상점로봇동의상태"]
    collector.run_collect(df=pd.DataFrame([only_consent_missing]))

    with SL() as s:
        logs = list(s.scalars(select(dbm.CollectLog).order_by(dbm.CollectLog.id)))
        assert [r.status for r in logs] == ["success"]          # fail 행을 만들지 않는다
        assert "상점로봇동의상태" in logs[-1].message            # 1개만 빠지면 이름이 그대로 보인다
        assert kpi.freshness(s)["last_failed"] is False         # 수집 실패로 보이면 안 된다
        assert s.get(dbm.RawOrder, "1").store_robot_consent is None

        collector.run_collect(df=pd.DataFrame([{**full, "배달ID": "2", "상점로봇동의상태": "미응답"}]))
        assert s.get(dbm.RawOrder, "2").store_robot_consent == "미응답"
        assert "쿼리에 없는 컬럼" not in list(
            s.scalars(select(dbm.CollectLog).order_by(dbm.CollectLog.id)))[-1].message


@pytest.fixture()
def mem_db(monkeypatch):
    """인메모리 DB 로 갈아끼운다(수집 경로가 쓰는 세션 팩토리 전부)."""
    SL = dbm.make_session_factory("sqlite://")
    monkeypatch.setattr(dbm, "SessionLocal", SL)
    monkeypatch.setattr(dbm, "get_session_factory", lambda: SL)
    monkeypatch.setattr(collector, "get_session_factory", lambda: SL)
    return SL


def _seeded(SL, **over):
    """동의 컬럼이 없던 시절에 수집된 모양의 행 1건."""
    o = collector.df_to_orders(pd.DataFrame([{**ROW, "배달ID": "D-1", **over}]))[0]
    with SL() as s:
        s.merge(o)
        s.commit()
    return o


def test_backfill_fills_only_added_columns(mem_db):
    """기존 행에 FILL_COLS 만 채우고 나머지 컬럼은 그대로 둔다. DB 에 없는 주문은 만들지 않는다."""
    SL = mem_db
    _seeded(SL)              # store_robot_consent / store_id / payment_method 가 NULL 인 과거 행
    with SL() as s:
        before = s.get(dbm.RawOrder, "D-1")
        assert before.store_robot_consent is None and before.store_id is None
        assert before.payment_method is None

    frame = pd.DataFrame([
        {"배달ID": "D-1", "상점ID": 7162, "상점로봇동의상태": "동의", "결제방식": "현장카드결제"},
        {"배달ID": "D-9", "상점ID": 226701, "상점로봇동의상태": "미응답", "결제방식": "선결제"},   # DB 에 없는 주문
    ])
    st = collector.backfill_added_columns(date(2026, 10, 1), date(2026, 10, 31),
                                          fetch=lambda a, b: frame)

    assert (st["matched"], st["updated"], st["not_in_db"]) == (1, 1, 1)
    with SL() as s:
        o = s.get(dbm.RawOrder, "D-1")
        assert (o.store_robot_consent, o.store_id) == ("동의", "7162")
        assert o.payment_method == "현장카드결제"
        assert o.s_order_finish == 1200 and o.delivery_status == "DROP_FINISHED"   # 그대로
        assert s.get(dbm.RawOrder, "D-9") is None                                  # 새로 안 만든다


def test_backfill_dry_run_and_no_collect_log(mem_db):
    """dry-run 은 건수만 세고 쓰지 않는다. 어느 쪽이든 collect_log 를 건드리지 않아
    '마지막 수집 시각'이 백필로 갱신되면 안 된다(데이터가 최신인 것처럼 보인다)."""
    from app import kpi
    SL = mem_db
    _seeded(SL)
    frame = pd.DataFrame([{"배달ID": "D-1", "상점ID": 1, "상점로봇동의상태": "미동의"}])

    st = collector.backfill_added_columns(date(2026, 10, 1), date(2026, 10, 31),
                                          dry_run=True, fetch=lambda a, b: frame)
    assert st["updated"] == 1
    with SL() as s:
        assert s.get(dbm.RawOrder, "D-1").store_robot_consent is None   # 저장 안 됨
        assert kpi.freshness(s)["last_success"] is None                 # 로그 없음

    collector.backfill_added_columns(date(2026, 10, 1), date(2026, 10, 31),
                                     fetch=lambda a, b: frame)
    with SL() as s:
        assert s.get(dbm.RawOrder, "D-1").store_robot_consent == "미동의"
        assert kpi.freshness(s)["last_success"] is None


def test_backfill_reports_missing_column_and_fills_the_rest(mem_db):
    """쿼리에 없는 컬럼은 조용히 비우지 말고 알려주되, **그 컬럼만** 건너뛴다.

    달 전체를 거르면 같이 받을 수 있던 나머지 컬럼까지 NULL 로 남는다.
    """
    SL = mem_db
    _seeded(SL, **{"상점로봇동의상태": "동의"})
    st = collector.backfill_added_columns(
        date(2026, 10, 1), date(2026, 10, 31),
        fetch=lambda a, b: pd.DataFrame([{"배달ID": "D-1", "상점ID": 1}]))
    assert st["no_col"] == {"상점로봇동의상태": ["2026-10"], "결제방식": ["2026-10"]}
    assert st["updated"] == 1                                            # 상점ID 는 채워졌다
    with SL() as s:
        o = s.get(dbm.RawOrder, "D-1")
        assert o.store_robot_consent == "동의" and o.payment_method is None   # 기존 값 유지
        assert o.store_id == "1"


def test_backfill_survives_one_month_failure(mem_db):
    """한 달 조회가 실패해도 나머지 달은 채운다(스캔 한도·타임아웃)."""
    SL = mem_db
    _seeded(SL)

    def fetch(a, b):
        if a.month == 9:
            raise collector.CollectError("스캔 한도 초과")
        return pd.DataFrame([{"배달ID": "D-1", "상점ID": 7162, "상점로봇동의상태": "동의"}])

    st = collector.backfill_added_columns(date(2026, 9, 1), date(2026, 10, 31), fetch=fetch)
    assert st["months"] == 2 and len(st["failed"]) == 1 and st["updated"] == 1
    with SL() as s:
        assert s.get(dbm.RawOrder, "D-1").store_robot_consent == "동의"


def test_consent_fill_by_month(mem_db):
    """백필 전후 비교에 쓰는 월별 채움 집계."""
    SL = mem_db
    with SL() as s:
        for i, (dt, consent) in enumerate([("2026-09-30 18:00:00", "동의"),
                                           ("2026-10-01 18:00:00", None),
                                           ("2026-10-02 18:00:00", "미응답")]):
            o = collector.df_to_orders(pd.DataFrame([
                {**ROW, "배달ID": f"X-{i}", "상점주문접수일시": dt,
                 "상점로봇동의상태": consent}]))[0]
            s.merge(o)
        s.commit()
        assert collector.consent_fill_by_month(s) == [("2026-09", 1, 1), ("2026-10", 2, 1)]


# ---------------------------------------------------------------- 상점 동의 현황(별도 쿼리)
CONSENT_ROW = {
    "storeId": 7162, "name": "가상상점", "상점구분": "일반(로드샵)", "관리주체": "self",
    "동의여부": "동의", "최초응답일": "2026-09-20", "동의일시": "2026-09-20 14:05",
    "최종변경일": "2026-10-01", "응답후변경": "Y", "주문건수": 12,
}


def test_df_to_store_consent_maps_columns():
    """상점ID 는 문자열로, 날짜는 날짜로. 주문건수는 기간 종속이라 저장하지 않는다."""
    r = collector.df_to_store_consent(pd.DataFrame([CONSENT_ROW]))[0]
    assert r.store_id == "7162" and r.store_name == "가상상점"
    assert r.store_type == "일반(로드샵)" and r.consent == "동의"
    assert r.first_response_date == date(2026, 9, 20) and r.last_changed_date == date(2026, 10, 1)
    assert r.agreed_at == datetime(2026, 9, 20, 14, 5) and r.changed_after_response == "Y"
    assert not hasattr(r, "order_count")
    with pytest.raises(collector.CollectError):          # 필수 컬럼이 없으면 멈춘다
        collector.df_to_store_consent(pd.DataFrame([{"name": "가상상점"}]))


def test_collect_store_consent_skipped_without_query_id(monkeypatch, mem_db):
    """쿼리 ID 가 없으면 조용히 건너뛴다 — 주문 수집만 쓰는 배포도 그대로 돌아야 한다."""
    monkeypatch.delenv("REDASH_CONSENT_QUERY_ID", raising=False)
    called = []
    assert collector.collect_store_consent(date(2026, 10, 1), date(2026, 10, 7),
                                           fetch=lambda *a, **k: called.append(1)) == (0, "")
    assert called == []


def test_collect_store_consent_upserts_and_notes_gap(monkeypatch, mem_db):
    monkeypatch.setenv("REDASH_CONSENT_QUERY_ID", "3535")
    frame = pd.DataFrame([CONSENT_ROW, {**CONSENT_ROW, "storeId": 99, "동의여부": "미응답"}])
    n, note = collector.collect_store_consent(date(2026, 10, 1), date(2026, 10, 7),
                                              fetch=lambda *a, **k: frame)
    assert n == 2 and note == " · 상점 동의 2곳"
    with mem_db() as s:
        assert s.get(dbm.StoreConsent, "99").consent == "미응답"

    # 컬럼이 빠진 응답은 적재는 하되 로그 문구로 알린다
    thin = pd.DataFrame([{k: v for k, v in CONSENT_ROW.items() if k != "최종변경일"}])
    _, note = collector.collect_store_consent(date(2026, 10, 1), date(2026, 10, 7),
                                              fetch=lambda *a, **k: thin)
    assert "최종변경일" in note


def test_run_collect_keeps_going_when_consent_query_fails(monkeypatch, mem_db):
    """동의 현황은 보조 지표다. 실패해도 주문 수집을 fail 로 떨구면 '수집 실패' 로 잘못 보인다."""
    from sqlalchemy import select

    from app import kpi
    monkeypatch.setenv("REDASH_CONSENT_QUERY_ID", "3535")
    monkeypatch.setattr(collector, "fetch_redash",
                        lambda *a, **k: (_ for _ in ()).throw(collector.CollectError("권한 없음")))
    today_dt = f"{date.today()} 18:00:00"
    collector.run_collect(df=pd.DataFrame([{**ROW, "배달ID": "9", "상점ID": "S-1",
                                            "상점로봇동의상태": "동의", "상점주문접수일시": today_dt}]))
    with mem_db() as s:
        last = list(s.scalars(select(dbm.CollectLog).order_by(dbm.CollectLog.id)))[-1]
        assert last.status == "success" and "상점 동의 수집 실패" in last.message
        assert kpi.freshness(s)["last_failed"] is False
        assert s.get(dbm.RawOrder, "9").store_robot_consent == "동의"      # 주문은 정상 적재
