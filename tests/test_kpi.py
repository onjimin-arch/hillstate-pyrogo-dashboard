import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm  # noqa: E402
from app import kpi  # noqa: E402
from app.config import CFG  # noqa: E402


def make_order(i, robot=False, store_type=None, hand=None, total=1200, day=30, hour=12,
               status="DROP_FINISHED"):
    return dbm.RawOrder(
        delivery_id=str(i), order_id=str(i), building=CFG["building"],
        delivery_type="로봇연계" if robot else "일반",
        store_type=store_type or CFG["store_types"][0], delivery_status=status,
        ord_dt=datetime(2026, 9, day, hour, 0), s_order_finish=total, s_order_pickup=total / 2,
        s_order_dispatch=30, s_dispatch_pickup=300, s_pickup_dockclose=300 if robot else None,
        s_dockclose_robotfinish=hand, robot_name="1호기" if robot else None)


@pytest.fixture()
def session(monkeypatch):
    SL = dbm.make_session_factory("sqlite://")
    monkeypatch.setattr(dbm, "SessionLocal", SL)
    with SL() as s:
        s.add_all([
            make_order(1),                                    # 일반 20분
            make_order(2, total=1800),                        # 일반 30분
            make_order(3, robot=True, hand=300, total=1500),  # 로봇 적시
            make_order(4, robot=True, hand=500, total=2100),  # 로봇 지연
            make_order(5, robot=True, hand=900, total=5000),  # 로봇 이상치(제외 대상)
            make_order(6, store_type="B2B"),                  # B2B도 모수 포함
            make_order(7, status="CANCELED"),                 # 완료 아님
        ])
        s.commit()
        yield s


def test_period_week_in_progress_and_d_minus_1():
    p = kpi.resolve_period("week", date(2026, 10, 1), None, None, today=date(2026, 10, 1))
    assert p.start == date(2026, 9, 28) and p.end == date(2026, 10, 4)
    assert p.eff_end == date(2026, 9, 30) and p.in_progress and p.elapsed_days == 3


def test_period_day_defaults_to_yesterday():
    p = kpi.resolve_period("day", None, None, None, today=date(2026, 10, 1))
    assert p.start == p.end == date(2026, 9, 30)


def test_scope_only_loadshop_and_completed(session):
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    assert {o.delivery_id for o in rows} == {"1", "2", "3", "4", "5", "6"}


def test_metrics_without_notes(session):
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    m = kpi.compute_metrics(rows, {})
    assert m["completed"] == 6 and m["robot_done"] == 3
    assert m["usage_pct"] == 50.0
    assert m["timely_n"] == 1 and m["timely_base"] == 3     # 300초만 7분 이내
    assert m["success_pct"] == 100.0


def test_exclusion_removes_from_all_metrics(session):
    session.add(dbm.RobotOrderNote(delivery_id="5", exclude_from_kpi=True, note="테스트 주문"))
    session.commit()
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    m = kpi.compute_metrics(rows, kpi.load_notes(session))
    assert m["completed"] == 5 and m["robot_done"] == 2 and m["usage_pct"] == 40.0   # 건수·이용률에서도 제외
    assert m["timely_base"] == 2 and m["excluded_n"] == 1
    assert m["avg_total_robot_min"] == round((1500 + 2100) / 2 / 60, 1)
    d = kpi.dashboard(session, kpi.resolve_period("day", date(2026, 9, 30), None, None, today=date(2026, 10, 1)))
    assert sum(d["daily"]["robot"]) + sum(d["daily"]["general"]) == 5                  # 차트에도 반영


def test_failure_counts_against_success(session):
    session.add(dbm.RobotOrderNote(delivery_id="4", result_type="실패", note="적재함 오류"))
    session.add(dbm.RobotOrderNote(delivery_id="5", result_type="기타"))
    session.commit()
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    m = kpi.compute_metrics(rows, kpi.load_notes(session))
    assert (m["success_n"], m["success_base"]) == (1, 2)      # '기타'는 분모 제외
    assert m["fail_n"] == 1 and m["noted_n"] == 1


def test_note_endpoint_and_upsert_keeps_notes(session):
    from fastapi.testclient import TestClient

    from app.collector import upsert_orders
    from app.main import app

    c = TestClient(app)
    r = c.post("/robots/4/note", data={"result_type": "실패", "note": "도킹 실패", "exclude": "1"},
               headers={"x-editor": "%ED%99%8D%EA%B8%B8%EB%8F%99"})
    assert r.status_code == 200 and "도킹 실패" in r.text
    n = session.get(dbm.RobotOrderNote, "4")
    session.refresh(n)
    assert n.exclude_from_kpi and n.result_type == "실패" and n.updated_by == "홍길동"
    assert c.post("/robots/1/note", data={"note": "x"}).status_code == 404   # 일반 건은 불가
    # 수집 upsert(롤링 리플레이)가 메모를 지우지 않는다
    upsert_orders([make_order(4, robot=True, hand=500, total=2100)])
    session.expire_all()
    assert session.get(dbm.RobotOrderNote, "4").note == "도킹 실패"


def test_pages_render(session):
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    for u in ("/", "/?grain=day", "/?grain=month", "/robots", "/robots?flt=failed"):
        assert c.get(u).status_code == 200, u


def test_annotation_counts_include_excluded(session):
    session.add(dbm.RobotOrderNote(delivery_id="3", result_type="실패", note="도킹 실패"))
    session.add(dbm.RobotOrderNote(delivery_id="4", result_type="기타", exclude_from_kpi=True))
    session.add(dbm.RobotOrderNote(delivery_id="5", note="메모만"))
    session.commit()
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    m = kpi.compute_metrics(rows, kpi.load_notes(session))
    assert (m["annotated_n"], m["noted_n"], m["fail_n"], m["other_n"], m["excluded_n"]) == (3, 2, 1, 1, 1)


def test_completed_with_error_counts_as_success_but_not_in_time(session):
    # 5번: 로봇 이상치(5000초) — 시스템 오류로 시간만 비정상인 완료 건
    session.add(dbm.RobotOrderNote(delivery_id="5", result_type="완료(오류)", note="이벤트 오입력"))
    session.commit()
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    m = kpi.compute_metrics(rows, kpi.load_notes(session))
    assert m["completed"] == 6 and m["robot_done"] == 3 and m["usage_pct"] == 50.0     # 건수·이용률 포함
    assert (m["success_n"], m["success_base"]) == (3, 3)                              # 성공에 포함
    assert m["timely_base"] == 2 and m["time_excluded_n"] == 1                        # 적시 모수에서 제외
    assert m["avg_total_robot_min"] == round((1500 + 2100) / 2 / 60, 1)               # 시간 평균에서 제외
    assert m["time_n"] == 5 and m["err_n"] == 1 and m["excluded_n"] == 0
    w = kpi.weekly_series(rows, kpi.load_notes(session), date(2026, 9, 30))
    assert w["robot_min"][-1] == round((1500 + 2100) / 2 / 60, 1)


def test_order_list_filters_and_miss_candidates(session):
    p = kpi.resolve_period("day", date(2026, 9, 30), None, None, today=date(2026, 10, 1))
    r = kpi.order_list(session, p)
    assert r["total"] == 7 and r["counts"]["robot"] == 3                    # B2B·취소 포함 전체
    assert r["counts"]["miss"] == 3                                          # 로봇 안 탄 로드샵 완료(1,2)
    assert kpi.order_list(session, p, "miss")["total"] == 3
    assert kpi.order_list(session, p, st="b2b")["total"] == 1
    assert kpi.order_list(session, p, q="3")["total"] >= 1


def test_order_note_endpoint_test_order_excluded_from_kpi(session):
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    c.post("/robots/4/note", data={"result_type": "실패"})
    r = c.post("/orders/1/note", data={"miss_reason": "테스트 주문", "note": "QA", "exclude": "1"})
    assert r.status_code == 200 and "테스트 주문" in r.text
    assert c.post("/orders/1/note", data={"miss_reason": "없는사유"}).status_code == 400
    assert c.post("/orders/999/note", data={}).status_code == 404
    # 일반 주문 메모가 로봇 건 결과 구분을 건드리지 않고, 제외된 테스트 주문은 집계에서 빠진다
    session.expire_all()
    assert session.get(dbm.RobotOrderNote, "4").result_type == "실패"
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    m = kpi.compute_metrics(rows, kpi.load_notes(session))
    assert m["completed"] == 5 and m["miss_n"] == 1 and m["excluded_n"] == 1
    assert c.get("/orders?grain=day&d=2026-09-30&flt=miss").status_code == 200


def test_migration_adds_miss_reason(tmp_path):
    import sqlite3
    f = tmp_path / "old.db"
    con = sqlite3.connect(f)
    con.execute("CREATE TABLE robot_order_notes (delivery_id VARCHAR PRIMARY KEY, result_type VARCHAR, note TEXT, "
                "exclude_from_kpi BOOLEAN, updated_at DATETIME, updated_by VARCHAR)")
    con.execute("INSERT INTO robot_order_notes VALUES ('9','기타','메모',1,NULL,NULL)")
    con.commit(); con.close()
    SL = dbm.make_session_factory(f"sqlite:///{f}")
    with SL() as s:
        n = s.get(dbm.RobotOrderNote, "9")
        assert n.note == "메모" and n.miss_reason is None


def test_daily_table_splits_loadshop_b2b_robot(session):
    """일자별 주요 항목의 완료 = 일반(로드샵) + B2B + 로봇연계 + 집계제외."""
    def day_metrics():
        rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
        day_rows = kpi.daily_table(rows, kpi.load_notes(session), date(2026, 9, 30), date(2026, 9, 30))
        return day_rows[0]["m"]

    m = day_metrics()
    assert (m["loadshop_done"], m["b2b_done"], m["robot_done"]) == (2, 1, 3)
    assert m["completed_all"] == 6 and m["excluded_n"] == 0

    # 집계제외 건이 생겨도 "완료" 열에서는 빠지지 않는다 (KPI 모수 completed 에서만 빠진다)
    session.add(dbm.RobotOrderNote(delivery_id="1", exclude_from_kpi=True))
    session.commit()
    m = day_metrics()
    assert m["excluded_n"] == 1 and m["completed"] == 5 and m["completed_all"] == 6
    assert m["completed_all"] == (m["loadshop_done"] + m["b2b_done"]
                                 + m["robot_done"] + m["excluded_n"])


def test_dashboard_renders_b2b_column(session):
    from fastapi.testclient import TestClient

    from app.main import app
    session.add(dbm.RobotOrderNote(delivery_id="1", exclude_from_kpi=True))
    session.commit()
    r = TestClient(app).get("/?grain=week&d=2026-09-30")   # 표는 grain != 'day' 에서만 렌더된다
    assert r.status_code == 200
    # 표: 합이 왼쪽에서 오른쪽으로 읽히도록 제외가 로봇연계 바로 뒤에 온다
    head = r.text[r.text.index("일자별 주요 항목"):]
    assert "<th>완료</th><th>일반</th><th>B2B</th><th>로봇연계</th><th>제외</th>" in head
    # 카드: 전체 완료 건수도 제외를 포함해 표와 숫자가 통일된다
    # 제외한 "1" 은 로드샵 건이라 로드샵에서 빠져 제외로 옮겨간다 (6 = 1+1+3+1)
    card = r.text[r.text.index("전체 완료 건수"):r.text.index("로봇 완료 건수")]
    assert "6<small>건</small>" in card
    assert "로드샵 1 · B2B 1 · 로봇연계 3 · 제외 1" in card


def _consent_order(i, store_id, consent, store_type=None, hour=12):
    o = make_order(i, store_type=store_type, hour=hour)
    o.store_id, o.store_robot_consent = store_id, consent
    return o


def test_consent_stats_by_shop_loadshop_only():
    rows = [
        _consent_order(1, "S1", "동의"), _consent_order(2, "S1", "동의"),     # 같은 상점 2건
        _consent_order(3, "S2", "미동의"), _consent_order(4, "S3", "미응답"),
        _consent_order(5, "0", "미응답"),                                     # 상점 정보 없음: 상점 수에서 제외
        _consent_order(6, "S9", "동의", store_type="B2B"),                    # B2B 는 로드샵 기준에서 제외
    ]
    c = kpi.consent_stats(rows)
    assert (c["consent_shops"], c["noconsent_shops"], c["noresp_shops"]) == (1, 1, 1)
    assert c["consent_shop_base"] == 3 and c["consent_shop_pct"] == 33.3
    assert (c["consent_order_n"], c["consent_order_base"]) == (2, 5)         # 주문 기준엔 store_id '0' 포함


def test_consent_stats_none_when_not_collected():
    """컬럼 추가 전 수집분(NULL)뿐이면 0% 가 아니라 None(화면에서 '-')."""
    c = kpi.consent_stats([make_order(1), make_order(2)])
    assert c["consent_shop_pct"] is None and c["consent_order_pct"] is None


def test_consent_in_metrics_and_order_row(session):
    o = session.get(dbm.RawOrder, "1")
    o.store_id, o.store_robot_consent = "S1", "동의"
    session.commit()
    p = kpi.resolve_period("custom", None, date(2026, 9, 30), date(2026, 9, 30), today=date(2026, 10, 2))
    assert kpi.dashboard(session, p)["metrics"]["consent_shops"] == 1
    assert kpi.order_row_view(o, None)["consent"] == "동의"


def test_daily_average_uses_elapsed_days(session):
    """일평균 = 건수 ÷ 경과일수(D-1 까지). 진행 중인 주도 지난 날짜만큼으로 나눈다."""
    p = kpi.resolve_period("custom", None, date(2026, 9, 28), date(2026, 10, 4), today=date(2026, 10, 1))
    assert p.elapsed_days == 3                                # 9/28~9/30
    m = kpi.dashboard(session, p)["metrics"]
    assert m["avg_days"] == 3
    assert m["avg_completed"] == round(m["completed_all"] / 3, 1)
    assert m["avg_robot"] == round(m["robot_done"] / 3, 1)


def test_daily_average_none_before_any_day_elapsed(session):
    p = kpi.resolve_period("week", date(2026, 10, 5), None, None, today=date(2026, 10, 5))   # 월요일 당일: 경과 0일
    m = kpi.dashboard(session, p)["metrics"]
    assert m["avg_days"] == 0 and m["avg_completed"] is None and m["avg_robot"] is None


def test_robot_only_pickup_average(session):
    """표의 로봇 기준 평균. 전체(일반+로봇)와 달리 로봇연계 건만 평균낸다. 시간 오류 건은 제외."""
    rows = [make_order(10, robot=True, hand=300, total=1000), make_order(11, robot=True, hand=300, total=2000),
            make_order(12, total=600)]
    m = kpi.compute_metrics(rows, {})
    assert m["avg_total_robot_min"] == round((1000 + 2000) / 2 / 60, 1)
    assert m["avg_pickup_robot_min"] == round(((1000 - 500) + (2000 - 1000)) / 2 / 60, 1)   # total - pickup(total/2)
    assert m["avg_total_min"] != m["avg_total_robot_min"]                                     # 전체 기준은 별도 유지
    assert kpi.compute_metrics([make_order(13)], {})["avg_pickup_robot_min"] is None         # 로봇 건 없으면 None
