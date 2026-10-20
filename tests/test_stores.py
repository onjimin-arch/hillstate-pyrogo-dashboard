"""상점 현황 화면 — 명부(store_consent) + 최근 1개월 주문건수(raw_orders).

핵심은 모수다. 전체는 '주문과 무관한 영업 대상 전부', 액티브는 '실제로 주문이 도는 상점'
이라 두 숫자가 섞이면 안 된다.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm, kpi  # noqa: E402
from app.config import CFG  # noqa: E402
from test_import import client, jwt  # noqa: E402,F401  (fixture 재사용)

TODAY = date(2026, 10, 20)          # data_end = 10-19, 최근 30일 = 09-20 ~ 10-19


def store(sid, consent, name=None, store_type="일반(로드샵)", changed=None, changed_date=None):
    return dbm.StoreConsent(store_id=sid, store_name=name or f"가상상점 {sid}",
                            store_type=store_type, consent=consent,
                            changed_after_response=changed, last_changed_date=changed_date,
                            collected_at=datetime(2026, 10, 20, 9, 0))


def order(did, store_id, day):
    return dbm.RawOrder(delivery_id=did, store_id=store_id, building=CFG["building"],
                        delivery_type="일반", store_type="일반(로드샵)",
                        delivery_status="DROP_FINISHED", ord_dt=datetime.combine(day, datetime.min.time()))


@pytest.fixture()
def session(monkeypatch):
    SL = dbm.make_session_factory("sqlite://")
    monkeypatch.setattr(dbm, "SessionLocal", SL)
    with SL() as s:
        yield s


def test_active_is_scoped_to_last_month(session):
    """액티브 = 최근 30일에 주문이 있던 상점. 31일 전 주문만 있는 상점은 빠진다."""
    for r in [store("S1", "동의"), store("S2", "미동의"), store("S3", "미응답")]:
        session.merge(r)
    session.add_all([
        order("D1", "S1", date(2026, 10, 18)),                 # 기간 안
        order("D2", "S1", date(2026, 9, 20)),                  # 기간 첫날(포함)
        order("D3", "S2", date(2026, 9, 19)),                  # 기간 밖(하루 차이)
        order("D4", "S3", date(2026, 10, 20)),                 # 당일 — D-1 까지만 보므로 제외
    ])
    session.commit()

    r = kpi.store_status(session, today=TODAY)
    assert (r["date_from"], r["date_to"]) == (date(2026, 9, 20), date(2026, 10, 19))
    assert r["all"]["base"] == 3
    assert r["active"]["base"] == 1 and r["active"]["ok"] == 1         # S1 만 액티브
    rows = {x["store_id"]: x for x in r["rows"]}
    assert rows["S1"]["orders"] == 2 and rows["S2"]["orders"] == 0 and rows["S3"]["orders"] == 0


def test_shares_and_zero_base(session):
    """비율은 각자의 모수 기준. 모수가 0이면 None 이어야 한다 — 0% 로 보이면 오해한다."""
    empty = kpi.store_status(session, today=TODAY)
    assert empty["all"]["base"] == 0 and empty["all"]["ok_pct"] is None

    for r in [store("S1", "동의"), store("S2", "동의"), store("S3", "미동의"), store("S4", "미응답")]:
        session.merge(r)
    session.add(order("D1", "S3", date(2026, 10, 18)))         # 미동의 상점만 액티브
    session.commit()

    r = kpi.store_status(session, today=TODAY)
    assert (r["all"]["ok"], r["all"]["ok_pct"]) == (2, 50.0)
    assert (r["all"]["no"], r["all"]["no_pct"]) == (1, 25.0)
    assert (r["all"]["none"], r["all"]["none_pct"]) == (1, 25.0)
    assert (r["active"]["base"], r["active"]["no"], r["active"]["no_pct"]) == (1, 1, 100.0)


def test_store_type_filter_and_unknown_consent(session):
    """상점구분 필터는 카드 모수까지 같이 좁힌다. 상태가 없는 상점은 세 칸 어디에도 안 들어간다."""
    for r in [store("S1", "동의"), store("S2", "동의", store_type="B2B"), store("S3", None)]:
        session.merge(r)
    session.commit()

    assert kpi.store_status(session, today=TODAY)["all"]["unknown"] == 1
    ls = kpi.store_status(session, today=TODAY, st="loadshop")
    assert ls["all"]["base"] == 2 and ls["all"]["ok"] == 1
    assert kpi.store_status(session, today=TODAY, st="b2b")["all"]["base"] == 1


def test_store_id_zero_is_not_a_store(session):
    """상점ID '0'(요기요 등 주문처 단위 B2B)은 상점이 아니라 모수에서 뺀다."""
    session.merge(store("0", "미응답", name=None, store_type="B2B"))
    session.merge(store("S1", "동의"))
    session.commit()
    r = kpi.store_status(session, today=TODAY)
    assert r["all"]["base"] == 1 and [x["store_id"] for x in r["rows"]] == ["S1"]


def test_sort_and_search(session):
    for sid, c, n in [("S1", "동의", "김밥집"), ("S2", "미동의", "분식집"), ("S3", "미응답", "커피집")]:
        session.merge(store(sid, c, name=n))
    session.add_all([order("D1", "S2", date(2026, 10, 1)), order("D2", "S2", date(2026, 10, 2)),
                     order("D3", "S3", date(2026, 10, 3))])
    session.commit()

    by_orders = kpi.store_status(session, today=TODAY)["rows"]         # 기본 정렬
    assert [r["store_id"] for r in by_orders] == ["S2", "S3", "S1"]
    asc = kpi.store_status(session, today=TODAY, desc=False)["rows"]
    assert asc[0]["store_id"] == "S1"
    by_consent = kpi.store_status(session, today=TODAY, sort="consent", desc=False)["rows"]
    assert [r["consent"] for r in by_consent] == ["동의", "미동의", "미응답"]
    found = kpi.store_status(session, today=TODAY, q="커피")
    assert found["total"] == 1 and found["rows"][0]["store_id"] == "S3"
    assert kpi.store_status(session, today=TODAY, q="S1")["total"] == 1       # 상점ID 로도 찾는다


def test_stores_page_renders(client):
    """화면: 명부가 없으면 안내, 있으면 카드와 표. 로그인 안 된 요청은 막힌다."""
    h = jwt("jmlee@barogo.com")
    page = client.get("/stores", headers=h)
    assert page.status_code == 200 and "상점 명부가 아직 없습니다" in page.text
    assert client.get("/stores").status_code == 401
    assert 'href="/stores"' in page.text                                      # 사이드바 메뉴

    SL = dbm.get_session_factory()
    with SL() as s:
        s.merge(store("S1", "동의", name="가상김밥"))
        s.merge(store("S2", "미응답", name="가상커피"))
        s.commit()
    page = client.get("/stores", headers=h).text
    assert "가상김밥" in page and "전체 상점 수" in page and "액티브 상점 수" in page
    assert "가상커피" not in client.get("/stores?q=김밥", headers=h).text


def test_gap_surfaces_orders_that_cannot_be_counted(session):
    """액티브가 적게 나오는 두 경로를 화면에서 드러내야 한다 — 숫자만 보면 '상점이 적다'로 오해한다.
    ① 상점ID 없이 수집된 과거 주문  ② 주문은 있는데 명부에 없는 상점."""
    session.merge(store("S1", "동의"))
    session.add_all([
        order("D1", "S1", date(2026, 10, 18)),          # 정상 집계
        order("D2", None, date(2026, 10, 17)),          # ① 상점ID 없음(과거 수집분)
        order("D3", "", date(2026, 10, 17)),            # ① 빈 문자열도 같은 취급
        order("D4", "0", date(2026, 10, 16)),           # 주문처 단위 B2B — 상점 아님
        order("D5", "S404", date(2026, 10, 15)),        # ② 명부에 없는 상점
        order("D6", "S404", date(2026, 10, 14)),
    ])
    session.commit()

    g = kpi.store_status(session, today=TODAY)["gap"]
    assert g["orders_total"] == 6
    assert g["no_store_id"] == 2 and g["agency_orders"] == 1
    assert (g["orphan_shops"], g["orphan_orders"]) == (1, 2)
    assert g["counted_orders"] == 1


def test_range_options(session):
    """기간 선택: 30일·90일·PoC 전체·수집 전 기간. 끝은 항상 D-1."""
    session.merge(store("S1", "동의"))
    session.add_all([order("D1", "S1", date(2026, 10, 18)),
                     order("D0", "S1", date(2026, 7, 1))])      # 30일·90일 밖, 전 기간 안
    session.commit()

    assert kpi.store_range(session, "30", TODAY)[:2] == (date(2026, 9, 20), date(2026, 10, 19))
    assert kpi.store_range(session, "90", TODAY)[0] == date(2026, 7, 22)
    assert kpi.store_range(session, "poc", TODAY)[0] == date(2026, 9, 29)
    assert kpi.store_range(session, "all", TODAY)[0] == date(2026, 7, 1)   # 가장 이른 주문
    assert kpi.store_range(session, "이상한값", TODAY)[2] == "30"           # 기본값으로 떨어진다

    assert kpi.store_status(session, today=TODAY, rng="30")["rows"][0]["orders"] == 1
    wide = kpi.store_status(session, today=TODAY, rng="all")
    assert wide["rows"][0]["orders"] == 2 and wide["rng"] == "all"


def test_range_chips_and_order_in_page(client):
    """화면: 액티브 섹션이 전체 명부 섹션보다 위. 기간 칩이 선택을 유지한다."""
    h = jwt("jmlee@barogo.com")
    SL = dbm.get_session_factory()
    with SL() as s:
        s.merge(store("S1", "동의", name="가상김밥"))
        s.commit()
    page = client.get("/stores", headers=h).text
    assert page.index("액티브 상점") < page.index("주문 유무와 무관한 전체 명부")
    assert 'href="/stores?rng=all"' in page
    assert "rng=poc" in client.get("/stores?rng=poc", headers=h).text


def test_active_excludes_b2b(session):
    """액티브는 로드샵만. B2B(주문처 단위)는 상점 단위 동의 대상이 아니라 전부 '미응답' 으로
    잡혀, 같이 세면 액티브 동의율이 실제보다 낮게 보인다."""
    session.merge(store("S1", "동의"))
    session.merge(store("S2", "미응답", store_type="B2B"))
    session.add_all([order("D1", "S1", date(2026, 10, 18)), order("D2", "S2", date(2026, 10, 18))])
    session.commit()

    r = kpi.store_status(session, today=TODAY)
    assert r["all"]["base"] == 2                        # 전체 명부에는 B2B 도 들어간다
    assert r["active"]["base"] == 1                     # 액티브는 로드샵만
    assert (r["active"]["ok"], r["active"]["ok_pct"]) == (1, 100.0)
    assert r["active"]["none"] == 0                     # B2B 미응답이 섞이면 50% 로 떨어진다
    # 표는 필터대로 B2B 도 보여준다(액티브 카드만 로드샵 기준)
    assert len(kpi.store_status(session, today=TODAY, st="b2b")["rows"]) == 1
