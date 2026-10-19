"""뉴빌리티 수기 시트 파싱 + 로봇 복귀·가용률."""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from test_import import client, jwt  # noqa: F401  (fixture 재사용)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm  # noqa: E402
from app import kpi, neubility as nb  # noqa: E402

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
