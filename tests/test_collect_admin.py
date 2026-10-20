import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm  # noqa: E402
from test_import import client, jwt  # noqa: E402,F401  (fixture 재사용)


def test_collect_status_and_access(client):
    h = jwt("jmlee@barogo.com")
    other = jwt("other@barogo.com")
    assert client.get("/admin/collect/status", headers=other).status_code == 403
    assert client.post("/admin/collect", headers={**other, "x-requested-with": "x"}).status_code == 403
    assert client.post("/admin/collect", headers=h).status_code == 400          # 커스텀 헤더 없음
    j = client.get("/admin/collect/status", headers=h).json()
    assert j["raw_orders"] == 0 and set(j["env"]) == {"REDASH_URL", "REDASH_API_KEY", "REDASH_QUERY_ID"}
    assert j["log"] == []
    assert "지금 수집" in client.get("/admin/import", headers=h).text


def test_manual_trigger_runs_once(client, monkeypatch):
    from app import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "run_collect", lambda full=False: calls.append(full) or 3)
    h = {**jwt("jmlee@barogo.com"), "x-requested-with": "x"}
    assert client.post("/admin/collect", headers=h).status_code == 202
    import time
    for _ in range(50):
        if not scheduler.STATE["running"] and calls:
            break
        time.sleep(0.05)
    assert calls == [True]            # 이력이 없으면 전체 백필


def test_consent_backfill_endpoint(client, monkeypatch):
    """동의상태 백필: 관리자만, 커스텀 헤더 필수, 결과는 상태 API 로 보인다."""
    import time
    from datetime import date

    from app import scheduler
    seen = {}

    def fake(start, end, dry_run=False, fetch=None):
        seen["range"] = (start, end)
        return {"months": 1, "fetched": 21, "matched": 20, "updated": 18, "not_in_db": 1,
                "no_consent_col": [], "failed": []}

    monkeypatch.setattr(scheduler, "backfill_consent", fake)
    monkeypatch.setattr(scheduler, "orders_date_range", lambda s: (date(2026, 8, 1), date(2026, 10, 18)))
    h = {**jwt("jmlee@barogo.com"), "x-requested-with": "x"}
    other = {**jwt("other@barogo.com"), "x-requested-with": "x"}
    assert client.post("/admin/collect/consent", headers=other).status_code == 403
    assert client.post("/admin/collect/consent", headers=jwt("jmlee@barogo.com")).status_code == 400
    assert client.post("/admin/collect/consent", headers=h).status_code == 202
    for _ in range(50):
        if not scheduler.STATE["running"] and "range" in seen:
            break
        time.sleep(0.05)
    assert seen["range"] == (date(2026, 8, 1), date(2026, 10, 18))
    msg = client.get("/admin/collect/status", headers=h).json()["last_consent"]
    assert "갱신 18건" in msg and "2026-08-01~2026-10-18" in msg
    assert "동의상태만 채우기" in client.get("/admin/import", headers=h).text


def test_status_reports_response_columns(client, monkeypatch):
    """'쿼리는 고쳤는데 화면이 비어 있다' 를 화면에서 판정할 수 있어야 한다.
    수집은 필수 컬럼만 검사하므로, 실제로 받은 컬럼을 raw_json 에서 되짚어 보여준다."""
    import json
    from datetime import datetime

    from app import collector, db as dbm

    monkeypatch.setenv("REDASH_QUERY_ID", "3531")
    h = jwt("jmlee@barogo.com")
    assert client.get("/admin/collect/status", headers=h).json()["cols"]["n"] == 0   # 수집 전

    sent = [c for c in dbm.ALL_COLS if c != "상점로봇동의상태"]       # 동의 컬럼만 빠진 응답
    SL = dbm.get_session_factory()
    with SL() as s:
        s.merge(dbm.RawOrder(delivery_id="D-1", collected_at=datetime.now(),
                             raw_json=json.dumps({c: "x" for c in sent}, ensure_ascii=False)))
        s.commit()
        assert collector.last_response_columns(s)["missing"] == ["상점로봇동의상태"]

    j = client.get("/admin/collect/status", headers=h).json()
    assert j["query_id"] == "3531"
    assert j["cols"]["missing"] == ["상점로봇동의상태"] and j["cols"]["n"] == len(sent)


def test_consent_backfill_leaves_a_log_row(client, monkeypatch):
    """백필 결과는 프로세스 메모리에만 두면 재배포 때 사라진다. collect_log 에 info 로 남기되
    '마지막 수집 시각'이나 '수집 실패' 표시는 건드리지 않아야 한다."""
    import time
    from datetime import date

    from app import kpi, scheduler
    monkeypatch.setattr(scheduler, "orders_date_range", lambda s: (date(2026, 8, 1), date(2026, 10, 19)))
    monkeypatch.setattr(scheduler, "backfill_consent", lambda *a, **k: {
        "months": 3, "fetched": 900, "matched": 880, "updated": 870, "not_in_db": 2,
        "no_consent_col": [], "failed": []})
    h = {**jwt("jmlee@barogo.com"), "x-requested-with": "x"}
    assert client.post("/admin/collect/consent", headers=h).status_code == 202
    for _ in range(50):
        if not scheduler.STATE["running"] and scheduler.STATE["last_consent"]:
            break
        time.sleep(0.05)

    rows = client.get("/admin/collect/status", headers=h).json()["log"]
    assert rows and rows[0]["status"] == "info" and "동의상태 백필" in rows[0]["message"]
    assert "갱신 870건" in rows[0]["message"]
    with dbm.get_session_factory()() as s:
        f = kpi.freshness(s)
        assert f["last_failed"] is False and f["last_success"] is None   # 수집 이력은 그대로
