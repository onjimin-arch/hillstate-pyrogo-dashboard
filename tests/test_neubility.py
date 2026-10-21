"""뉴빌리티 수기 시트 파싱 + 로봇 복귀·가용률."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from test_import import client, jwt  # noqa: F401  (fixture 재사용)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm  # noqa: E402
from app import kpi, neubility as nb  # noqa: E402
from app.config import CFG  # noqa: E402

# 실제 시트와 같은 열 구성. 헤더 오타('딛힌')와 '-' 결측을 그대로 둔다.
HDR = ("일자,상태,로봇 ID,가게명,목적지,로봇 매칭 시각,로봇_도킹존 도착시각,"
       "(도킹존) 적재함 열림 시각,(도킹존) 적재함 딛힌 시각,(도킹존) 출발시각,(동앞) 도착 시각,"
       "(세대 앞) 적재함 열림 시각,(세대 앞) 적재함 닫힌 시각,(도킹존) 복귀시작 시각,"
       "(도킹존) 복귀완료 시각,로봇 배송완료 시각,전체배송 수행시간,비고")


def sheet(*rows):
    return ("\n".join([HDR, *rows])).encode("utf-8")


def row(day="26.09.29", status="완료", robot="229", dest="999동 9999호",
        dock_close="19:08", unit_close="19:14", ret_start="19:15", ret_done="19:18", note=""):
    return (f"{day},{status},{robot},가상상점,{dest},18:55,18:55,19:07,{dock_close},19:08,19:12,"
            f"19:14,{unit_close},{ret_start},{ret_done},0:06,0:11,{note}")


@pytest.fixture()
def session(monkeypatch):
    SL = dbm.make_session_factory("sqlite://")
    monkeypatch.setattr(dbm, "SessionLocal", SL)
    with SL() as s:
        yield s


def test_parse_basic_segments():
    trips, stat = nb.parse(sheet(row()))
    assert stat["parsed"] == 1 and len(trips) == 1
    t = trips[0]
    assert t.ord_date == date(2026, 9, 29) and t.robot_id == "229"
    assert t.s_dockclose_finish == 360          # 19:08 → 19:14
    assert t.s_finish_return_start == 60        # 19:14 → 19:15
    assert t.s_return == 180                    # 19:15 → 19:18
    assert t.s_cycle == 600                     # 19:08 → 19:18


def test_unit_number_is_not_stored():
    """호수(개별 세대)는 저장하지 않는다. 동만 남는다."""
    trips, _ = nb.parse(sheet(row(dest="998동 9998호")))        # 가상 값(실제 세대 아님)
    assert trips[0].dong == "998"
    assert "호" not in (trips[0].dong or "")
    assert all("9998" not in str(v) for v in vars(trips[0]).values() if isinstance(v, str))


def test_skips_rider_direct_and_empty_rows():
    blob = sheet(row(), row(status="라이더_직배송"), ",,,,,,,,,,,,,,,0:00,0:00,",
                 row(dock_close="-", unit_close="-", ret_start="-", ret_done="-"))
    trips, stat = nb.parse(blob)
    assert stat["parsed"] == 1
    assert stat["skipped_status"] == 1 and stat["skipped_empty"] == 1 and stat["skipped_no_time"] == 1
    assert [t.status for t in trips] == ["완료"]


def test_midnight_crossing_vs_reversed_entry():
    """자정 넘김은 하루를 더하고, 몇 분짜리 역전(수기 오류)은 값으로 치지 않는다."""
    over, _ = nb.parse(sheet(row(dock_close="23:55", unit_close="00:05",
                                 ret_start="00:07", ret_done="00:12")))
    assert over[0].s_dockclose_finish == 600            # 23:55 → 00:05 = 10분
    assert over[0].s_cycle == 1020                      # 17분, 24시간이 아니다

    bad, stat = nb.parse(sheet(row(dock_close="19:08", unit_close="19:14",
                                   ret_start="19:15", ret_done="19:04")))
    assert stat["time_reversed"] == 1
    assert bad[0].s_return is None and bad[0].s_cycle is None   # 음수는 버린다
    assert bad[0].s_dockclose_finish == 360                     # 멀쩡한 구간은 남는다


def test_reupload_overwrites_same_trip(session):
    trips, _ = nb.parse(sheet(row(note="최초")))
    nb.upsert(session, trips, "a@barogo.com")
    again, _ = nb.parse(sheet(row(note="수정본")))
    nb.upsert(session, again, "b@barogo.com")
    rows = list(session.scalars(select(dbm.RobotTrip)))
    assert len(rows) == 1 and rows[0].note == "수정본" and rows[0].uploaded_by == "b@barogo.com"


def test_availability_metrics(session):
    blob = sheet(
        row(robot="229", dock_close="19:08", unit_close="19:14", ret_start="19:15", ret_done="19:18"),
        row(robot="229", day="26.09.30", dock_close="14:00", unit_close="14:10",
            ret_start="14:12", ret_done="14:20"),
        row(robot="231", day="26.09.30", dock_close="20:00", unit_close="20:06",
            ret_start="20:08", ret_done="20:14"),
        row(status="라이더_직배송"),
    )
    nb.upsert(session, nb.parse(blob)[0], "a@barogo.com")
    p = kpi.resolve_period("custom", None, date(2026, 9, 29), date(2026, 9, 30),
                           today=date(2026, 10, 1))
    a = kpi.robot_availability(session, p)
    assert a["n"] == 3 and a["robots"] == 2 and a["days"] == 2
    assert a["cycle_min"] == 14.0                 # 10 / 20 / 14 분 → 중앙값 14
    assert a["return_min"] == 6.0                 # 3 / 8 / 6 분 → 중앙값 6
    assert a["after_min"] == 8.0                  # 4 / 10 / 8 분 → 중앙값 8
    assert a["after_pct"] == round(8 / 14 * 100, 1)
    assert [r["robot_id"] for r in a["by_robot"]] == ["229", "231"]
    assert next(r for r in a["by_robot"] if r["robot_id"] == "229")["n"] == 2


def test_availability_empty_period(session):
    p = kpi.resolve_period("day", date(2026, 1, 1), None, None, today=date(2026, 1, 2))
    assert kpi.robot_availability(session, p)["n"] == 0


def test_bad_sheet_raises():
    with pytest.raises(nb.SheetError):
        nb.parse(b"a,b,c\n1,2,3\n")               # 열 이름이 다름
    with pytest.raises(nb.SheetError):
        nb.parse(sheet())                         # 헤더만 있고 행이 없음


def test_upload_access(client):
    """업로드는 관리자만. 일반 사용자는 403, 로그인 헤더가 없으면 401."""
    files = {"file": ("sheet.csv", sheet(row()), "text/csv")}
    assert client.post("/admin/trips/upload", files=files).status_code == 401
    assert client.post("/admin/trips/upload", files=files,
                       headers=jwt("other@barogo.com")).status_code == 403
    r = client.post("/admin/trips/upload", files=files, headers=jwt("jmlee@barogo.com"))
    assert r.status_code == 200 and "1건 적재" in r.text


def test_upload_rejects_garbage(client):
    h = jwt("jmlee@barogo.com")
    assert client.post("/admin/trips/upload", files={"file": ("x.csv", b"", "text/csv")},
                       headers=h).status_code == 400
    bad = b"a,b\n1,2\n"                     # 열 이름이 시트와 다르면 400
    assert client.post("/admin/trips/upload", files={"file": ("x.csv", bad, "text/csv")},
                       headers=h).status_code == 400


# ---------------------------------------------------------------- 주문 ↔ 시트 동 붙이기
def _robot_order(did, dock, fin, robot="바로고 3호기 [231호기]"):
    """로봇 건 한 줄. 시트와 달리 Redash 시각에는 초가 있다."""
    return dbm.RawOrder(
        delivery_id=did, building=CFG["building"], delivery_type="로봇연계",
        store_type=CFG["store_types"][0], delivery_status="DROP_FINISHED",
        robot_name=robot, ord_dt=dock - timedelta(minutes=20),
        dock_close_dt=dock, robot_finish_dt=fin,
        s_dockclose_robotfinish=(fin - dock).total_seconds())


def test_dong_matches_across_minute_rounding(session):
    """시트는 분 단위라 Redash 와 수십 초 어긋난다 — 그래도 같은 건으로 붙어야 한다."""
    o = _robot_order("D1", datetime(2026, 9, 29, 19, 8, 41), datetime(2026, 9, 29, 19, 14, 12))
    session.add(o)
    nb.upsert(session, nb.parse(sheet(row(robot="231", dest="999동 9999호")))[0], "a@barogo.com")
    assert kpi.dong_map(session, [o]) == {"D1": "999"}
    assert kpi.row_view(o, None, kpi.dong_for(session, o))["dong"] == "999"


def test_dong_empty_when_sheet_missing_or_far_off(session):
    """시트에 없는 건, 그리고 3분 넘게 어긋난 건에는 붙이지 않는다(옆 회차 오인 방지)."""
    o1 = _robot_order("D1", datetime(2026, 9, 29, 19, 8, 41), datetime(2026, 9, 29, 19, 14, 12))
    o2 = _robot_order("D2", datetime(2026, 9, 29, 19, 20, 0), datetime(2026, 9, 29, 19, 26, 0))
    session.add_all([o1, o2])
    assert kpi.dong_map(session, [o1, o2]) == {}        # 업로드분 없음
    nb.upsert(session, nb.parse(sheet(row(robot="231", dest="999동 9999호")))[0], "a@barogo.com")
    assert kpi.dong_map(session, [o1, o2]) == {"D1": "999"}


def test_dong_one_trip_per_order(session):
    """같은 로봇의 두 회차가 서로 바뀌지 않고, 한 시트 행은 한 주문에만 붙는다."""
    o1 = _robot_order("D1", datetime(2026, 9, 29, 19, 8, 20), datetime(2026, 9, 29, 19, 14, 50))
    o2 = _robot_order("D2", datetime(2026, 9, 29, 19, 40, 10), datetime(2026, 9, 29, 19, 46, 30))
    session.add_all([o1, o2])
    blob = sheet(row(robot="231", dest="901동 101호", dock_close="19:08", unit_close="19:14",
                     ret_start="19:15", ret_done="19:18"),
                 row(robot="231", dest="902동 202호", dock_close="19:40", unit_close="19:46",
                     ret_start="19:47", ret_done="19:52"))
    nb.upsert(session, nb.parse(blob)[0], "a@barogo.com")
    assert kpi.dong_map(session, [o1, o2]) == {"D1": "901", "D2": "902"}


def test_dong_matches_when_robot_naming_differs(session):
    """시트가 `3호기`, Redash 가 `[231호기]` 처럼 번호 체계가 아예 다르면 시각만으로 맞춘다."""
    o = _robot_order("D1", datetime(2026, 9, 29, 19, 8, 41), datetime(2026, 9, 29, 19, 14, 12))
    session.add(o)
    nb.upsert(session, nb.parse(sheet(row(robot="3호기", dest="903동 303호")))[0], "a@barogo.com")
    assert kpi.dong_map(session, [o]) == {"D1": "903"}


def test_dong_respects_robot_when_numbers_line_up(session):
    """번호 체계가 맞을 때는 로봇이 다르면 붙이지 않는다 — 시각이 비슷해도 다른 로봇 건이다."""
    o = _robot_order("D1", datetime(2026, 9, 29, 19, 8, 41), datetime(2026, 9, 29, 19, 14, 12),
                     robot="바로고 1호기 [229호기]")
    o2 = _robot_order("D2", datetime(2026, 9, 29, 19, 8, 35), datetime(2026, 9, 29, 19, 14, 5),
                      robot="바로고 3호기 [231호기]")
    session.add_all([o, o2])
    blob = sheet(row(robot="229", dest="901동 101호"), row(robot="231", dest="902동 202호"))
    nb.upsert(session, nb.parse(blob)[0], "a@barogo.com")
    assert kpi.dong_map(session, [o, o2]) == {"D1": "901", "D2": "902"}


def test_dong_coverage_after_upload(session):
    o = _robot_order("D1", datetime(2026, 9, 29, 19, 8, 41), datetime(2026, 9, 29, 19, 14, 12))
    o2 = _robot_order("D2", datetime(2026, 9, 29, 21, 0, 0), datetime(2026, 9, 29, 21, 6, 0))
    session.add_all([o, o2])
    nb.upsert(session, nb.parse(sheet(row(robot="231", dest="999동 9999호")))[0], "a@barogo.com")
    assert kpi.dong_coverage(session, date(2026, 9, 29), date(2026, 9, 29)) == {
        "orders": 2, "matched": 1}
