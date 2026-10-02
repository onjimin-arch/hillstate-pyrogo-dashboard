import base64
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm  # noqa: E402
from app import importer  # noqa: E402

ORDERS = dbm.RawOrder.__table__
NOTES = dbm.RobotOrderNote.__table__


def make_src(path, n_orders=2500):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE raw_orders (delivery_id TEXT PRIMARY KEY, building TEXT, ord_dt TEXT, "
              "s_order_finish REAL, dispatch_count INTEGER, extra_col TEXT)")
    c.executemany("INSERT INTO raw_orders VALUES (?,?,?,?,?,?)",
                  [(str(i), "B", "2026-09-30 12:34:56.000000", 1200.5, 1, "x") for i in range(n_orders)])
    c.execute("CREATE TABLE robot_order_notes (delivery_id TEXT PRIMARY KEY, exclude_from_kpi BOOLEAN, "
              "updated_at DATETIME, note TEXT)")
    c.executemany("INSERT INTO robot_order_notes VALUES (?,?,?,?)",
                  [("1", 1, "2026-10-01 09:00:00", "n"), ("2", 0, None, None)])
    c.execute("CREATE TABLE collect_log (id INTEGER PRIMARY KEY, ts DATETIME, status TEXT, rows INTEGER, message TEXT)")
    c.execute("INSERT INTO collect_log VALUES (7, '2026-10-01 01:00:00', 'success', 5, NULL)")
    c.execute("CREATE TABLE unrelated (a INTEGER)")
    c.commit()
    c.close()


@pytest.fixture()
def engine():
    return dbm.make_session_factory("sqlite://").kw["bind"]


def last_event(events, table):
    return [e for e in events if e.get("table") == table and "inserted" in e][-1]


def test_import_converts_chunks_and_skips_existing(tmp_path, engine):
    src = tmp_path / "src.db"
    make_src(src)
    with engine.begin() as c:       # 이미 있는 id 는 건너뛰고 기존 값을 보존해야 한다
        c.execute(ORDERS.insert().values(delivery_id="5", building="KEEP"))
    events = []
    importer.run_import(src, engine, lambda **kw: events.append(kw))

    with engine.connect() as c:
        assert len(c.execute(select(ORDERS)).all()) == 2500
        assert c.execute(select(ORDERS.c.building).where(ORDERS.c.delivery_id == "5")).scalar() == "KEEP"
        o = c.execute(select(ORDERS.c.ord_dt, ORDERS.c.dispatch_count).where(ORDERS.c.delivery_id == "9")).one()
        assert (o.ord_dt.year, o.ord_dt.hour, o.dispatch_count) == (2026, 12, 1)
        notes = {r.delivery_id: r for r in c.execute(select(NOTES))}
        assert notes["1"].exclude_from_kpi is True and notes["2"].exclude_from_kpi is False
        assert notes["1"].updated_at.day == 1 and notes["2"].updated_at is None
        assert c.execute(select(dbm.CollectLog.__table__.c.id)).scalar() == 7
    last = last_event(events, "raw_orders")
    assert (last["read"], last["inserted"], last["skipped"]) == (2500, 2499, 1)
    assert sum(1 for e in events if e.get("table") == "raw_orders" and "read" in e) == 3   # 1000+1000+500


def test_import_is_rerunnable(tmp_path, engine):
    src = tmp_path / "src.db"
    make_src(src, 10)
    importer.run_import(src, engine)
    ev = []
    importer.run_import(src, engine, lambda **kw: ev.append(kw))
    last = last_event(ev, "raw_orders")
    assert (last["inserted"], last["skipped"]) == (0, 10)


def test_bad_date_fails_with_row_id(tmp_path, engine):
    src = tmp_path / "bad.db"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE raw_orders (delivery_id TEXT PRIMARY KEY, ord_dt TEXT)")
    c.execute("INSERT INTO raw_orders VALUES ('A1', 'not-a-date')")
    c.commit()
    c.close()
    with pytest.raises(ValueError, match="A1"):
        importer.run_import(src, engine)


def test_converters():
    b = importer.converter(NOTES.c.exclude_from_kpi)
    assert b(1) is True and b("0") is False and b(None) is None
    assert importer.converter(ORDERS.c.ord_dt)("2026-10-01T09:00:00Z").hour == 9


# ---------- 접근 제어·업로드 ----------
def jwt(email):
    b = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return {"x-amzn-oidc-data": f"h.{b}.s"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(dbm, "SessionLocal", dbm.make_session_factory("sqlite://"))
    monkeypatch.setattr(importer, "UPLOAD_DIR", tmp_path / "up")
    monkeypatch.setattr(importer, "JOB", None)
    with TestClient(main.app) as c:
        yield c


def test_only_admin_email_can_open(client):
    assert client.get("/admin/import").status_code == 403
    assert client.get("/admin/import", headers=jwt("other@barogo.com")).status_code == 403
    assert client.get("/admin/import", headers={"x-editor": "jmlee@barogo.com"}).status_code == 403
    assert client.get("/admin/import/status", headers=jwt("other@barogo.com")).status_code == 403
    r = client.post("/admin/import/upload", content=b"x", headers={**jwt("other@barogo.com"), "x-filename": "a"})
    assert r.status_code == 403
    ok = client.get("/admin/import", headers=jwt("jmlee@barogo.com"))
    assert ok.status_code == 200 and "가져오기 시작" in ok.text
    assert "데이터 가져오기" not in client.get("/", headers=jwt("other@barogo.com")).text


def test_upload_rejects_non_sqlite_and_missing_header(client):
    h = jwt("jmlee@barogo.com")
    assert client.post("/admin/import/upload", content=b"hello world, not sqlite at all",
                       headers={**h, "x-filename": "a.db"}).status_code == 400
    assert client.post("/admin/import/upload", content=b"x", headers=h).status_code == 400
    assert not list(importer.UPLOAD_DIR.glob("*.db"))          # 임시 파일 정리됨


def test_upload_runs_in_background(client, tmp_path):
    src = tmp_path / "src.db"
    make_src(src, 30)
    h = {**jwt("jmlee@barogo.com"), "x-filename": "src.db"}
    assert client.post("/admin/import/upload", content=src.read_bytes(), headers=h).status_code == 202
    for _ in range(100):
        j = client.get("/admin/import/status", headers=h).json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done" and j["inserted"] == 33 and j["total"] == 33
    assert not list(importer.UPLOAD_DIR.glob("*.db"))
