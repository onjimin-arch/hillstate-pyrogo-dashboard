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
    """일자별 주요 항목의 완료 = 일반(로드샵) + B2B + 로봇연계 (B2B 가 총합에 포함된다)."""
    rows = kpi.scoped_done(session, date(2026, 9, 30), date(2026, 9, 30))
    notes = kpi.load_notes(session)
    day_rows = kpi.daily_table(rows, notes, date(2026, 9, 30), date(2026, 9, 30))
    m = day_rows[0]["m"]
    assert (m["loadshop_done"], m["b2b_done"], m["robot_done"]) == (2, 1, 3)
    assert m["completed"] == m["loadshop_done"] + m["b2b_done"] + m["robot_done"]


def test_dashboard_renders_b2b_column(session):
    from fastapi.testclient import TestClient

    from app.main import app
    r = TestClient(app).get("/?grain=week&d=2026-09-30")   # 표는 grain != 'day' 에서만 렌더된다
    assert r.status_code == 200
    head = r.text[r.text.index("일자별 주요 항목"):]
    assert "<th>완료</th><th>일반</th><th>B2B</th><th>로봇연계</th>" in head
