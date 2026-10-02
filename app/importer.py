"""로컬 SQLite 파일 → 현재 DB(Postgres) 데이터 가져오기.

- 업로드 파일은 디스크 임시 파일로만 다룬다(메모리에 통째로 올리지 않는다).
- 테이블은 CHUNK 행씩 읽어 청크마다 커밋한다. 이미 있는 PK 는 건너뛴다(재실행 안전).
- 작업은 백그라운드 스레드에서 돌고, 진행 상태는 프로세스 메모리(JOB)에 둔다. 컨테이너가 1개라는 전제.
"""
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, Integer, text
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect

from .db import Base

log = logging.getLogger("dashboard.import")

CHUNK = 1000
SQLITE_MAGIC = b"SQLite format 3\x00"
UPLOAD_DIR = Path(tempfile.gettempdir()) / "pyrogo-import"

_lock = threading.Lock()
JOB: dict | None = None        # 가장 최근 작업 1건. 읽는 쪽은 snapshot() 사용.


# ---------- 값 변환 ----------
_TRUE = {"1", "t", "true", "y", "yes"}
_FALSE = {"0", "f", "false", "n", "no", ""}


def _to_bool(v):
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"불리언으로 바꿀 수 없는 값: {v!r}")


def _to_datetime(v):
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if not s:
            return None
        dt = datetime.fromisoformat(s)          # 'YYYY-MM-DD HH:MM:SS[.ffffff]', 'T' 구분, 'Z' 허용
    return dt.replace(tzinfo=None) if dt.tzinfo else dt     # 컬럼이 timezone 없는 DateTime


def _to_int(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"정수가 아닌 값: {v!r}")
        return int(v)
    s = str(v).strip() if isinstance(v, str) else v
    if s == "":
        return None
    return int(float(s)) if isinstance(s, str) and "." in s else int(s)


def converter(col):
    """모델 컬럼 타입에 맞춰 SQLite 값을 변환하는 함수를 돌려준다. None 은 그대로 None."""
    t = col.type
    if isinstance(t, Boolean):
        fn = _to_bool
    elif isinstance(t, DateTime):
        fn = _to_datetime
    elif isinstance(t, Integer):
        fn = _to_int
    elif isinstance(t, Float):
        fn = float
    else:
        fn = lambda v: v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)  # noqa: E731
    return lambda v: None if v is None else fn(v)


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------- 가져오기 ----------
def plan_tables(src: sqlite3.Connection):
    """모델에 정의된 테이블 중 SQLite 에도 있는 것 → [(Table, [사용할 컬럼])]."""
    present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    plan, skipped = [], []
    for table in Base.metadata.sorted_tables:       # SQL 에는 모델 쪽 이름만 쓴다(업로드 파일의 이름은 대조용)
        if table.name not in present:
            continue
        src_cols = {r[1] for r in src.execute(f"PRAGMA table_info({_qi(table.name)})")}
        cols = [c for c in table.columns if c.name in src_cols]
        pk_missing = [c.name for c in table.primary_key.columns if c.name not in src_cols]
        if pk_missing:
            skipped.append(f"{table.name}: 파일에 PK 컬럼({', '.join(pk_missing)})이 없어 건너뜀")
            continue
        plan.append((table, cols))
    return plan, skipped


def _insert_stmt(engine, table):
    ins = postgresql.insert if engine.dialect.name == "postgresql" else sqlite_dialect.insert
    return ins(table).on_conflict_do_nothing()


def sync_sequence(conn, table) -> None:
    """id 컬럼에 시퀀스가 있으면 현재 최댓값으로 맞춘다(Postgres 만)."""
    if conn.dialect.name != "postgresql" or "id" not in table.columns:
        return
    seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table.name}).scalar()
    if not seq:
        return
    mx = conn.execute(text(f"SELECT MAX(id) FROM {_qi(table.name)}")).scalar()
    if mx is not None:
        conn.execute(text("SELECT setval(:s, :m, true)"), {"s": seq, "m": mx})


def run_import(path: Path, engine, progress=None) -> dict:
    """path 의 SQLite 를 engine 의 DB 로 옮긴다. progress(dict) 는 갱신마다 호출."""
    prog = progress or (lambda **kw: None)
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        plan, skipped = plan_tables(src)
        if not plan:
            raise ValueError("가져올 수 있는 테이블이 없습니다(앱 테이블과 같은 이름의 테이블 없음)")
        totals = {t.name: src.execute(f"SELECT COUNT(*) FROM {_qi(t.name)}").fetchone()[0]
                  for t, _ in plan}
        prog(total=sum(totals.values()), tables={
            t.name: {"total": totals[t.name], "read": 0, "inserted": 0, "skipped": 0, "state": "대기"}
            for t, _ in plan}, notes=skipped)

        for table, cols in plan:
            conv = [converter(c) for c in cols]
            names = [c.name for c in cols]
            pk = names.index(table.primary_key.columns.keys()[0])
            stmt = _insert_stmt(engine, table).returning(table.primary_key.columns.values()[0])
            cur = src.execute(f"SELECT {', '.join(_qi(n) for n in names)} FROM {_qi(table.name)}")
            read = inserted = 0
            prog(table=table.name, table_state="진행 중")
            while True:
                chunk = cur.fetchmany(CHUNK)
                if not chunk:
                    break
                rows = []
                for raw in chunk:
                    try:
                        rows.append({n: f(v) for n, f, v in zip(names, conv, raw)})
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"{table.name} id={raw[pk]!r}: {e}") from None
                with engine.begin() as conn:                       # 청크마다 커밋
                    n_ins = len(conn.execute(stmt, rows).all())    # RETURNING: 실제로 들어간 행만
                read += len(rows)
                inserted += n_ins
                prog(table=table.name, read=read, inserted=inserted, skipped=read - inserted)
            with engine.begin() as conn:
                sync_sequence(conn, table)
            prog(table=table.name, table_state="완료")
        return {"tables": len(plan)}
    finally:
        src.close()


# ---------- 작업 상태 ----------
def snapshot() -> dict | None:
    with _lock:
        if JOB is None:
            return None
        out = {k: v for k, v in JOB.items() if k != "tables"}
        out["tables"] = {k: dict(v) for k, v in JOB.get("tables", {}).items()}
        out["notes"] = list(JOB.get("notes", []))
        tabs = out["tables"].values()
        out["read"] = sum(t["read"] for t in tabs)
        out["inserted"] = sum(t["inserted"] for t in tabs)
        out["skipped"] = sum(t["skipped"] for t in tabs)
        out["elapsed"] = round((JOB.get("ended") or time.time()) - JOB["started"])
        return out


def _update(table=None, table_state=None, read=None, inserted=None, skipped=None, **top):
    with _lock:
        if JOB is None:
            return
        if "tables" in top:
            JOB["tables"] = top.pop("tables")
        if "notes" in top:
            JOB["notes"] = top.pop("notes")
        JOB.update(top)
        t = JOB.get("tables", {}).get(table) if table else None
        if t is not None:
            if table_state is not None:
                t["state"] = table_state
            for k, v in (("read", read), ("inserted", inserted), ("skipped", skipped)):
                if v is not None:
                    t[k] = v


def is_running() -> bool:
    with _lock:
        return JOB is not None and JOB["status"] in ("uploading", "running")


def new_upload_path() -> Path | None:
    """업로드를 받을 새 임시 경로. 이미 작업이 진행 중이면 None."""
    global JOB
    with _lock:
        if JOB is not None and JOB["status"] in ("uploading", "running"):
            return None
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for old in UPLOAD_DIR.iterdir():            # 이전 작업이 남긴 파일 정리
            old.unlink(missing_ok=True)
        jid = uuid.uuid4().hex[:12]
        JOB = {"id": jid, "status": "uploading", "started": time.time(), "ended": None,
               "total": 0, "tables": {}, "notes": [], "error": None, "filename": None}
        return UPLOAD_DIR / f"{jid}.db"


def abort_upload(path: Path, msg: str) -> None:
    path.unlink(missing_ok=True)
    _update(status="failed", error=msg, ended=time.time())


def start(path: Path, engine, filename: str) -> None:
    """업로드가 끝난 파일로 백그라운드 가져오기를 시작한다."""
    _update(status="running", filename=filename)

    def work():
        try:
            run_import(path, engine, _update)
            _update(status="done", ended=time.time())
            log.info("import done file=%s", filename)
        except Exception as e:                      # noqa: BLE001 — 화면에 실패로 보여준다
            log.exception("import failed")
            first = (str(e).splitlines() or [type(e).__name__])[0][:300]
            _update(status="failed", error=f"{type(e).__name__}: {first}", ended=time.time())
        finally:
            path.unlink(missing_ok=True)

    threading.Thread(target=work, name="sqlite-import", daemon=True).start()


def max_upload_bytes() -> int:
    return int(os.getenv("IMPORT_MAX_MB", "500")) * 1024 * 1024
