import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
