import base64
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Body
from fastapi.responses import JSONResponse
from html import escape as html_escape
from starlette.concurrency import run_in_threadpool

from . import ai, importer, kpi, scheduler
from .config import CFG, ROOT
from sqlalchemy import func, select

from .db import AppUser, CollectLog, RawOrder, RobotOrderNote, get_session_factory

BASE = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")     # 로컬 개발용. 배포 환경에서는 플랫폼이 환경변수를 주입한다.

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dashboard")


def current_user(request: Request) -> str | None:
    """회사 로그인(ALB OIDC)이 붙이는 x-amzn-oidc-data(JWT)의 email 클레임. 자체 로그인은 두지 않는다."""
    tok = request.headers.get("x-amzn-oidc-data")
    if tok:
        try:
            payload = tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if claims.get("email"):
                return str(claims["email"])
        except (IndexError, ValueError):
            log.warning("x-amzn-oidc-data 해석 실패")
    # 로컬 개발·로그인 헤더가 없을 때만: 화면에 입력한 이름 → 접속 IP
    return unquote(request.headers.get("x-editor") or "").strip() or (
        request.client.host if request.client else None)


def get_db():
    SL = get_session_factory()
    with SL() as s:
        yield s


# 기본 관리자: DB 에 등록하지 않아도 항상 관리자(잠금 방지). 추가는 환경변수 ADMIN_EMAILS(쉼표 구분).
BOOTSTRAP_ADMINS = {"jmlee@barogo.com"} | {
    e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
ROLES = {"admin": "관리자", "user": "사용자"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def oidc_email(request: Request) -> str | None:
    """x-amzn-oidc-data 의 email 만 본다. current_user 와 달리 x-editor·IP 대체값은 없다(권한 판단용)."""
    tok = request.headers.get("x-amzn-oidc-data")
    if not tok:
        return None
    try:
        payload = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    return email.strip().lower() if isinstance(email, str) else None


class AccessDenied(Exception):
    def __init__(self, status: int, msg: str, email: str | None = None):
        self.status, self.msg, self.email = status, msg, email


def auth_gate(request: Request, db=Depends(get_db)) -> None:
    """모든 라우트 공통: 회사 로그인 이메일이 등록된 사용자(또는 기본 관리자)일 때만 통과."""
    if request.url.path == "/health":
        return
    email = oidc_email(request)
    if email is None:
        if os.getenv("AUTH_DISABLED") == "1":      # 로컬 개발 전용: 로그인 헤더가 없는 요청만 관리자로 취급
            request.state.email, request.state.role = "local", "admin"
            return
        # 플랫폼 헬스체크(`/`, 로그인 헤더 없음)는 200 으로 통과시키되 화면에는 아무 데이터도 보이지 않는다.
        raise AccessDenied(200 if request.url.path == "/" else 401, "회사 로그인이 필요합니다.")
    if email in BOOTSTRAP_ADMINS:
        role = "admin"
    else:
        u = db.get(AppUser, email)
        role = u.role if u else None
    if role is None:
        log.warning("미등록 사용자 접근 거부 email=%s path=%s", email, request.url.path)
        raise AccessDenied(403, "등록된 사용자만 접속할 수 있습니다. 관리자에게 등록을 요청하세요.", email)
    request.state.email, request.state.role = email, role


def require_admin(request: Request) -> str:
    if getattr(request.state, "role", None) != "admin":
        log.warning("관리자 전용 접근 거부 email=%s path=%s", getattr(request.state, "email", None),
                    request.url.path)
        raise HTTPException(403, "관리자만 사용할 수 있습니다")
    return request.state.email


require_import_admin = require_admin     # 데이터 가져오기·수집 화면도 관리자 전용


@asynccontextmanager
async def lifespan(_app):
    with get_session_factory()() as s:
        ai.ensure_views(s)       # AI가 조회하는 읽기 전용 뷰
    scheduler.start()            # Redash 주기 수집(AUTO_COLLECT=0 으로 끔)
    yield
    scheduler.stop()


app = FastAPI(title="로봇 배송 대시보드", lifespan=lifespan, dependencies=[Depends(auth_gate)])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _date(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _fmt_dt(v, f="%m-%d %H:%M"):
    return v.strftime(f) if v else "-"


def _fmt_num(v, nd=1, suffix=""):
    return "-" if v is None else f"{v:.{nd}f}{suffix}"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["num"] = _fmt_num


def _period_ctx(grain, d, start, end):
    p = kpi.resolve_period(grain if grain in ("day", "week", "month", "custom") else "week",
                           _date(d), _date(start), _date(end))
    return p


def _common(request: Request, p, db, active):
    return {
        "request": request, "p": p, "cfg": CFG, "active": active,
        "me": {"email": getattr(request.state, "email", None), "role": getattr(request.state, "role", None)},
        "can_import": getattr(request.state, "role", None) == "admin",
        "fresh": kpi.freshness(db), "today": date.today(),
        "grains": [("day", "일"), ("week", "주"), ("month", "월"), ("custom", "직접 선택")],
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, grain: str = "week", d: str | None = None,
          start: str | None = None, end: str | None = None, db=Depends(get_db)):
    p = _period_ctx(grain, d, start, end)
    ctx = _common(request, p, db, "dashboard")
    ctx.update(kpi.dashboard(db, p))
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/robots", response_class=HTMLResponse)
def robots(request: Request, grain: str = "week", d: str | None = None,
           start: str | None = None, end: str | None = None,
           flt: str | None = None, db=Depends(get_db)):
    p = _period_ctx(grain, d, start, end)
    ctx = _common(request, p, db, "robots")
    ctx.update({"rows": kpi.robot_list(db, p, flt), "flt": flt or "",
                "result_types": kpi.RESULT_TYPES})
    return templates.TemplateResponse(request, "robots.html", ctx)


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request, grain: str = "day", d: str | None = None,
           start: str | None = None, end: str | None = None, flt: str | None = None,
           st: str | None = None, q: str | None = None, page: int = 1, db=Depends(get_db)):
    p = _period_ctx(grain, d, start, end)
    ctx = _common(request, p, db, "orders")
    res = kpi.order_list(db, p, flt, st, q, page)
    extra = (f"&st={st}" if st else "") + (f"&q={quote(q)}" if q else "")
    ctx.update(res, flt=flt or "", st=st or "", q=q or "", miss_reasons=kpi.MISS_REASONS,
               extra_qs=extra, base_path="/orders")
    return templates.TemplateResponse(request, "orders.html", ctx)


@app.post("/orders/{delivery_id}/note", response_class=HTMLResponse)
def save_order_note(request: Request, delivery_id: str, miss_reason: str = Form(""),
                    note: str = Form(""), exclude: str | None = Form(None), db=Depends(get_db)):
    o = db.get(RawOrder, delivery_id)
    if o is None:
        raise HTTPException(404, "주문이 없습니다")
    if miss_reason and miss_reason not in kpi.MISS_REASONS:
        raise HTTPException(400, "잘못된 사유 구분")
    n = db.get(RobotOrderNote, delivery_id) or RobotOrderNote(delivery_id=delivery_id)
    n.miss_reason = miss_reason or None            # result_type(로봇 결과 구분)은 건드리지 않는다
    n.note = note.strip() or None
    n.exclude_from_kpi = exclude is not None
    n.updated_at = datetime.now()
    n.updated_by = current_user(request)
    db.merge(n)
    db.commit()
    return templates.TemplateResponse(
        request, "_order_row.html",
        {"r": kpi.order_row_view(o, n), "miss_reasons": kpi.MISS_REASONS, "saved": True})


@app.post("/robots/{delivery_id}/note", response_class=HTMLResponse)
def save_note(request: Request, delivery_id: str, result_type: str = Form(""),
              note: str = Form(""), exclude: str | None = Form(None), db=Depends(get_db)):
    o = db.get(RawOrder, delivery_id)
    if o is None or o.delivery_type != kpi.ROBOT:
        raise HTTPException(404, "로봇연계 건이 아닙니다")
    if result_type and result_type not in kpi.RESULT_TYPES:
        raise HTTPException(400, "잘못된 결과 구분")
    n = db.get(RobotOrderNote, delivery_id) or RobotOrderNote(delivery_id=delivery_id)
    n.result_type = result_type or None
    n.note = note.strip() or None
    n.exclude_from_kpi = exclude is not None
    n.updated_at = datetime.now()
    n.updated_by = current_user(request)
    db.merge(n)
    db.commit()
    log.info("robot note saved delivery_id=%s by=%s", delivery_id, n.updated_by)
    return templates.TemplateResponse(
        request, "_robot_row.html",
        {"r": kpi.row_view(o, n), "result_types": kpi.RESULT_TYPES, "saved": True})


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, db=Depends(get_db)):
    p = kpi.resolve_period("week", None, None, None)
    ctx = _common(request, p, db, "ask")
    ctx["ai_ready"] = bool(os.getenv("OPENAI_API_KEY", "").strip())
    return templates.TemplateResponse(request, "ask.html", ctx)


@app.post("/api/ask")
def api_ask(payload: dict = Body(...), db=Depends(get_db)):
    try:
        return ai.ask(db, payload.get("question", ""), payload.get("history"))
    except ai.AIError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/admin/import", response_class=HTMLResponse)
def import_page(request: Request, db=Depends(get_db), _=Depends(require_import_admin)):
    p = kpi.resolve_period("week", None, None, None)
    ctx = _common(request, p, db, "import")
    ctx["max_mb"] = importer.max_upload_bytes() // (1024 * 1024)
    return templates.TemplateResponse(request, "import.html", ctx)


@app.get("/admin/import/status")
def import_status(_=Depends(require_import_admin)):
    return JSONResponse(importer.snapshot() or {"status": "idle"}, headers={"Cache-Control": "no-store"})


@app.get("/admin/collect/status")
def collect_status(db=Depends(get_db), _=Depends(require_import_admin)):
    logs = db.scalars(select(CollectLog).order_by(CollectLog.ts.desc()).limit(10)).all()
    n_orders = db.scalar(select(func.count()).select_from(RawOrder))
    return JSONResponse({**scheduler.status(), "raw_orders": n_orders, "log": [
        {"ts": r.ts.strftime("%m-%d %H:%M:%S"), "status": r.status, "rows": r.rows,
         "message": (r.message or "")[:300]} for r in logs]}, headers={"Cache-Control": "no-store"})


@app.post("/admin/collect", status_code=202)
def collect_now(request: Request, full: bool = False, user=Depends(require_import_admin)):
    if "x-requested-with" not in request.headers:     # 커스텀 헤더 필수(CSRF 방지)
        raise HTTPException(400, "잘못된 요청")
    if not scheduler.trigger(full=full):
        raise HTTPException(409, "이미 수집이 진행 중입니다")
    log.info("manual collect by=%s full=%s", user, full)
    return {"status": "running"}


@app.post("/admin/import/upload", status_code=202)
async def import_upload(request: Request, user=Depends(require_import_admin)):
    """본문(raw)을 청크 단위로 디스크에 스트리밍한 뒤 백그라운드 가져오기를 시작하고 바로 돌려준다."""
    if "x-filename" not in request.headers:       # 커스텀 헤더 필수 — 다른 사이트의 폼 전송 차단
        raise HTTPException(400, "잘못된 요청")
    limit = importer.max_upload_bytes()
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, f"파일이 너무 큽니다(최대 {limit // 1024 // 1024}MB)")
    path = importer.new_upload_path()
    if path is None:
        raise HTTPException(409, "이미 가져오기가 진행 중입니다")
    size = 0
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                if size == 0 and not chunk.startswith(importer.SQLITE_MAGIC[:len(chunk)]):
                    raise ValueError("SQLite 파일이 아닙니다")
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"파일이 너무 큽니다(최대 {limit // 1024 // 1024}MB)")
                await run_in_threadpool(f.write, chunk)
        if size < len(importer.SQLITE_MAGIC):
            raise ValueError("SQLite 파일이 아닙니다")
    except Exception as e:                        # noqa: BLE001 — 끊김·검증 실패 모두 임시 파일 정리
        importer.abort_upload(path, str(e) if isinstance(e, ValueError) else "업로드가 중단되었습니다")
        if isinstance(e, ValueError):
            raise HTTPException(400, str(e))
        raise
    name = unquote(request.headers.get("x-filename") or "")[:200]
    log.info("import start by=%s file=%s bytes=%d", user, name, size)
    importer.start(path, get_session_factory().kw["bind"], name)
    return {"status": "running"}


@app.exception_handler(AccessDenied)
async def access_denied(request: Request, exc: AccessDenied):
    if "text/html" in request.headers.get("accept", ""):
        html = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>로봇 배송 대시보드</title><body style='font:15px system-ui,sans-serif;"
                "max-width:480px;margin:15vh auto;padding:0 20px'><h2>접근할 수 없습니다</h2>"
                f"<p>{exc.msg}</p>" + (f"<p style='color:#666'>접속 계정: {html_escape(exc.email)}</p>" if exc.email else "")
                + "</body>")
        return HTMLResponse(html, status_code=exc.status)
    return JSONResponse({"detail": exc.msg}, status_code=exc.status)


# ---------------------------------------------------------------- 설정 · 사용자 관리
def _user_rows(db):
    rows = [{"email": e, "role": "admin", "name": "기본 관리자", "fixed": True, "created_at": None, "created_by": None}
            for e in sorted(BOOTSTRAP_ADMINS)]
    for u in db.scalars(select(AppUser).order_by(AppUser.created_at)):
        if u.email not in BOOTSTRAP_ADMINS:
            rows.append({"email": u.email, "role": u.role, "name": u.name or "", "fixed": False,
                         "created_at": u.created_at, "created_by": u.created_by})
    return rows


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db=Depends(get_db)):
    p = kpi.resolve_period("week", None, None, None)
    ctx = _common(request, p, db, "settings")
    ctx.update(roles=ROLES, users=_user_rows(db) if ctx["can_import"] else [])
    return templates.TemplateResponse(request, "settings.html", ctx)


def _admin_write(request: Request) -> str:
    me = require_admin(request)
    if "x-requested-with" not in request.headers:     # 커스텀 헤더 필수(CSRF 방지)
        raise HTTPException(400, "잘못된 요청")
    return me


def _norm_email(v) -> str:
    e = str(v or "").strip().lower()
    if len(e) > 254 or not EMAIL_RE.match(e):
        raise HTTPException(400, "이메일 형식이 올바르지 않습니다")
    return e


def _norm_role(v) -> str:
    if v not in ROLES:
        raise HTTPException(400, "권한은 관리자/사용자 중 하나여야 합니다")
    return v


@app.post("/settings/users")
def user_save(request: Request, payload: dict = Body(...), db=Depends(get_db)):
    """사용자 등록(이미 있으면 권한·이름 수정)."""
    me = _admin_write(request)
    email, role = _norm_email(payload.get("email")), _norm_role(payload.get("role", "user"))
    if email in BOOTSTRAP_ADMINS:
        raise HTTPException(400, "기본 관리자는 변경할 수 없습니다")
    if email == me:
        raise HTTPException(400, "본인 계정은 변경할 수 없습니다")
    u = db.get(AppUser, email) or AppUser(email=email, created_at=datetime.now(), created_by=me)
    u.role = role
    if "name" in payload:
        u.name = (str(payload["name"] or "").strip()[:100]) or None
    db.merge(u)
    db.commit()
    log.info("user saved email=%s role=%s by=%s", email, role, me)
    return {"ok": True}


@app.post("/settings/users/delete")
def user_delete(request: Request, payload: dict = Body(...), db=Depends(get_db)):
    me = _admin_write(request)
    email = _norm_email(payload.get("email"))
    if email in BOOTSTRAP_ADMINS or email == me:
        raise HTTPException(400, "기본 관리자와 본인 계정은 삭제할 수 없습니다")
    u = db.get(AppUser, email)
    if u is None:
        raise HTTPException(404, "등록된 사용자가 아닙니다")
    db.delete(u)
    db.commit()
    log.info("user deleted email=%s by=%s", email, me)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
