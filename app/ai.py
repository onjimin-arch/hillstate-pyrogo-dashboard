"""AI 질의 (ChatGPT API, 함수 호출).

AI는 숫자를 직접 만들지 않고 두 도구로만 데이터를 조회한다.
  - get_kpi   : 대시보드와 동일한 집계 로직(집계 제외 반영)
  - query_sql : 읽기 전용 SELECT (허용된 뷰 ai_orders / ai_robot_notes 만 접근)
라이더ID·원본 JSON은 뷰에서 제외해 외부(OpenAI)로 나가지 않게 한다.
"""
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime

import requests
import sqlglot
from sqlglot import exp

from . import kpi
from .config import CFG

VIEWS = {"ai_orders", "ai_robot_notes"}
MAX_ROWS = 200
MAX_STEPS = 6
SQL_TIMEOUT_SEC = 3


class AIError(Exception):
    pass


# ------------------------------------------------------------------ 뷰
def ensure_views(session) -> None:
    b = CFG["building"].replace("'", "''")
    st = ", ".join("'" + t.replace("'", "''") + "'" for t in CFG["store_types"])
    conn = session.connection()
    from sqlalchemy import text
    if conn.dialect.name == "postgresql":
        ord_date = "TO_CHAR(ord_dt, 'YYYY-MM-DD')"
        ord_hour = "CAST(EXTRACT(HOUR FROM ord_dt) AS INTEGER)"
    else:
        ord_date = "date(ord_dt)"
        ord_hour = "CAST(strftime('%H', ord_dt) AS INTEGER)"
    conn.execute(text("DROP VIEW IF EXISTS ai_orders"))
    conn.execute(text(f"""
        CREATE VIEW ai_orders AS SELECT
          o.delivery_id, order_id, robot_delivery_id,
          {ord_date} AS ord_date, ord_dt, {ord_hour} AS ord_hour,
          building, delivery_type, store_type, robot_matched, robot_name, store_name, store_id, store_robot_consent, order_source,
          dispatch_count, order_status, delivery_status,
          dispatch_dt, pickup_dt, dock_close_dt, robot_finish_dt, finish_dt,
          s_order_dispatch, s_dispatch_pickup, s_order_pickup, s_pickup_dockclose,
          s_dockclose_robotfinish, s_order_finish,
          n.result_type AS result_type, n.miss_reason AS miss_reason, n.note AS note,
          CAST(COALESCE(n.exclude_from_kpi, FALSE) AS INTEGER) AS exclude_from_kpi,
          CASE WHEN building = '{b}' AND store_type IN ({st}) AND delivery_status = 'DROP_FINISHED'
                    AND NOT COALESCE(n.exclude_from_kpi, FALSE)
               THEN 1 ELSE 0 END AS in_kpi_scope,
          CASE WHEN building = '{b}' AND store_type IN ({st}) AND delivery_status = 'DROP_FINISHED'
                    AND NOT COALESCE(n.exclude_from_kpi, FALSE) AND COALESCE(n.result_type, '') <> '완료(오류)'
               THEN 1 ELSE 0 END AS in_time_scope
        FROM raw_orders o LEFT JOIN robot_order_notes n ON n.delivery_id = o.delivery_id"""))
    conn.execute(text("DROP VIEW IF EXISTS ai_robot_notes"))
    conn.execute(text("""
        CREATE VIEW ai_robot_notes AS SELECT
          delivery_id, result_type, note, exclude_from_kpi, updated_at, updated_by
        FROM robot_order_notes"""))
    session.commit()


# ------------------------------------------------------------------ SQL 도구
def _authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        if arg1 in VIEWS or source in VIEWS:      # 뷰 직접 조회 또는 뷰 내부 전개
            return sqlite3.SQLITE_OK
        if arg1 in ("raw_orders", "robot_order_notes") and not arg2:
            return sqlite3.SQLITE_OK              # COUNT(*) 계획용 컬럼 없는 접근(값 노출 없음)
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def _validate(sql: str, dialect: str) -> str | None:
    """단일 SELECT 이고, 허용된 뷰(와 WITH 별칭)만 참조하며, 알 수 없는 함수가 없는지 구문 분석으로 확인."""
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as e:
        return f"SQL 구문 오류: {e}"
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return "SELECT 문만 허용됩니다"
    tree = trees[0]
    if tree.find(exp.Into):
        return "SELECT INTO 는 허용되지 않습니다"
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if t.db or t.catalog or not (name in VIEWS or name in ctes):
            return f"허용되지 않은 테이블: {t.sql()}"
    if tree.find(exp.Anonymous):
        return "허용되지 않은 함수가 포함되어 있습니다"
    return None


def run_sql(session, sql: str) -> dict:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql or ";" in sql:
        return {"error": "SQL은 한 개의 SELECT 문만 허용됩니다"}
    if not re.match(r"(?is)^(select|with)\b", sql):
        return {"error": "SELECT 문만 허용됩니다"}
    dialect = session.get_bind().dialect.name
    err = _validate(sql, "postgres" if dialect == "postgresql" else "sqlite")
    if err:
        return {"error": err}
    if dialect == "postgresql":
        return _run_pg(session, sql)
    return _run_sqlite(session, sql)


def _run_pg(session, sql: str) -> dict:
    """별도 연결의 읽기 전용 트랜잭션 + statement_timeout 으로 실행."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    try:
        with session.get_bind().connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {SQL_TIMEOUT_SEC * 1000}"))
            res = conn.exec_driver_sql(sql)
            cols = list(res.keys())
            rows = res.fetchmany(MAX_ROWS + 1)
            conn.rollback()
    except SQLAlchemyError as e:
        return {"error": f"SQL 실행 실패: {getattr(e, 'orig', e)}"}
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {"columns": cols, "rows": [[_json(v) for v in r] for r in rows],
            "row_count": len(rows), "truncated": truncated}


def _run_sqlite(session, sql: str) -> dict:
    conn = session.connection().connection.driver_connection
    t0 = time.time()
    conn.execute("PRAGMA query_only=ON")
    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(lambda: 1 if time.time() - t0 > SQL_TIMEOUT_SEC else 0, 10000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        return {"error": f"SQL 실행 실패: {e}"}
    finally:
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)
        conn.execute("PRAGMA query_only=OFF")
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {"columns": cols, "rows": [[_json(v) for v in r] for r in rows],
            "row_count": len(rows), "truncated": truncated}


def _json(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


# ------------------------------------------------------------------ KPI 도구
def tool_get_kpi(session, grain="week", anchor=None, start=None, end=None) -> dict:
    def d(v):
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None
    p = kpi.resolve_period(grain if grain in ("day", "week", "month", "custom") else "week",
                           d(anchor), d(start), d(end))
    rows = kpi.scoped_done(session, p.start, p.eff_end)
    notes = kpi.load_notes(session)
    m = kpi.compute_metrics(rows, notes)
    noted = [{"delivery_id": o.delivery_id, "ord_date": o.ord_dt.date().isoformat(),
              "result_type": notes[o.delivery_id].result_type or "정상",
              "note": notes[o.delivery_id].note, "excluded": bool(notes[o.delivery_id].exclude_from_kpi)}
             for o in rows if o.delivery_id in notes and (notes[o.delivery_id].note or notes[o.delivery_id].result_type
                                                          or notes[o.delivery_id].exclude_from_kpi)][:30]
    return {"period": {"grain": p.grain, "start": p.start.isoformat(), "end": p.end.isoformat(),
                       "data_through": p.eff_end.isoformat(), "in_progress": p.in_progress},
            "metrics": m, "noted_robot_orders": noted,
            "baseline": CFG["baseline"], "targets": CFG["targets"]}


# ------------------------------------------------------------------ 프롬프트
def system_prompt(session) -> str:
    f = kpi.freshness(session)
    last = f["last_success"].strftime("%Y-%m-%d %H:%M") if f["last_success"] else "수집 이력 없음"
    from sqlalchemy import func, select
    from .db import RawOrder
    lo, hi = session.execute(select(func.min(RawOrder.ord_dt), func.max(RawOrder.ord_dt))).one()
    cover = (f"{lo:%Y-%m-%d} ~ {hi:%Y-%m-%d}" if lo else "데이터 없음")
    return f"""너는 '{CFG['building']}' 로봇 배송 PoC 대시보드의 데이터 분석 어시스턴트다. 사용자는 바로고 경영·현장 담당자다.
오늘은 {date.today():%Y-%m-%d}, 데이터는 전일(D-1)까지이며 마지막 수집은 {last}이다.
DB에 수집된 주문 데이터의 범위는 접수일 기준 {cover}이다.

## 원칙
- 질문 기간이 수집 범위({cover}) 밖이거나 일부만 걸치면 "0건"이라고 답하지 말고 "수집된 데이터 없음(수집 범위 외)"이라고 밝히고, 범위 안의 값만 답한다. 월별 등 기간별 집계는 query_sql로 ord_date 기준 GROUP BY 하여 실제 존재하는 기간을 확인한 뒤 답한다.
- 숫자는 반드시 도구(get_kpi, query_sql)로 조회한 값만 쓴다. 추측·기억으로 만들지 않는다. 데이터가 없으면 없다고 말한다.
- KPI(완료 건수, 평균 시간, 적시 배송률, 성공률, 이용률)는 get_kpi를 우선 쓴다(대시보드와 같은 값, 집계 제외 반영). 그 외 자유 질문(특이사항 정리, 시간대, 상점·주문처·기체별 등)은 query_sql을 쓴다.
- 기본 모수는 상점구분 {', '.join(CFG['store_types'])} + 완료(DROP_FINISHED). 상점구분별로 나눠 묻는 경우 store_type 으로 구분한다. 다른 모수를 쓰면 답에 밝힌다.
- 비율·평균에는 건수(n)를 함께 적는다. 일 단위처럼 표본이 작으면(n<10) 해석에 주의하라고 덧붙인다.
- 집계 제외(exclude_from_kpi=1)로 체크된 건은 모든 KPI 수치에서 이미 빠져 있다. 제외 건이 있으면 몇 건이고 사유(특이사항)가 무엇인지 함께 언급한다.
- 로봇으로 처리했어야 했는데 놓친 주문은 delivery_type='일반'인 모수 내 완료 건 중 miss_reason 이 기록된 건이다. '누락', '테스트 주문', '매칭 실패' 질문은 miss_reason·note 로 집계한다.
- 사용자가 입력한 특이사항(비고)·결과 구분(정상/실패/기타)·집계 제외는 ai_orders 의 note, result_type, exclude_from_kpi 컬럼(또는 get_kpi 의 noted_robot_orders)에 있다. '특이사항', '비고', '실패 사유', '제외된 건' 질문은 반드시 이 값을 조회해 근거로 쓴다.
- 결과 구분: '완료(오류)'는 로봇이 실제로는 정상 배송했지만 시스템 이벤트 오류로 시간 데이터가 비정상인 건이다. 건수·이용률·성공률에는 포함하고 평균 시간·적시 배송률·구간 평균에서만 제외한다(SQL로 시간 평균을 낼 땐 in_time_scope=1 조건 사용). '실패'는 성공률 분모에 포함·분자 제외, '기타'는 성공률 분모에서 제외.
- 시간은 DB에 초 단위다. 답은 분 단위(소수 1자리)로 환산한다.
- 로봇 배송 성공률은 '정의 확정 전' 잠정값(수기 입력한 결과 구분 기준)이다. 언급할 때 그 점을 밝힌다.
- 한국어로 간결하게 답한다. 핵심 결론을 먼저, 근거 수치를 뒤에 쓴다. 마크다운 표는 쓰지 말고 짧은 목록을 쓴다.

## 지표 정의
- 완료 건수: 모수 중 delivery_status='DROP_FINISHED'. 로봇 완료 건수: delivery_type='로봇연계'. 로봇 이용률 = 로봇연계 ÷ 전체 완료(로드샵+B2B).
- 접수→완료 = s_order_finish, 픽업→완료 = s_order_finish - s_order_pickup.
- 로봇 적시 배송률 = s_dockclose_robotfinish <= {CFG['timely_threshold_sec']}초(7분) 건수 ÷ 로봇연계 건수 (목표 {CFG['targets']['timely_pct']}%).
- 8월 baseline: 완료 {CFG['baseline']['orders']}건, 접수→완료 {CFG['baseline']['total_min']}분, 픽업→완료 {CFG['baseline']['pickup_min']}분. PoC 기간: {CFG['poc']['start']} ~ {CFG['poc']['end']}.
- 날짜 기준은 접수 시각(ord_dt, KST). 주는 월~일.

## query_sql 용 뷰 (PostgreSQL)
ai_orders(주문 1건 1행): delivery_id, order_id, robot_delivery_id, ord_date(YYYY-MM-DD), ord_dt, ord_hour(0-23),
  building, delivery_type('일반'|'로봇연계'), store_type('일반(로드샵)'|'B2B'), robot_matched, robot_name, store_name, store_id, store_robot_consent('동의'|'미동의'|'미응답'|NULL=수집 전), order_source,
  dispatch_count, order_status, delivery_status, dispatch_dt, pickup_dt, dock_close_dt, robot_finish_dt, finish_dt,
  s_order_dispatch, s_dispatch_pickup, s_order_pickup, s_pickup_dockclose, s_dockclose_robotfinish, s_order_finish (모두 초),
  miss_reason(일반 주문의 로봇 누락 사유: '시스템 오류(로봇 매칭 실패)'|'테스트 주문'|'로봇 운영 외(점검·미운영)'|'상점 미동의'|'기타'|NULL), result_type(로봇연계 건의 결과 구분: NULL 또는 '정상'=정상(완료) | '완료(오류)' | '실패' | '기타'), note(특이사항), exclude_from_kpi(1=집계 제외; 사용자 입력, 로봇연계 건만 값 존재),
  in_kpi_scope (1=건수·이용률·성공률 모수: 이 단지+로드샵·B2B+완료+집계 제외 아님),
  in_time_scope (1=평균 시간·적시 배송률 모수: in_kpi_scope 이면서 result_type이 '완료(오류)'가 아님)
ai_robot_notes(사용자 입력 원본): delivery_id, result_type, note, exclude_from_kpi, updated_at, updated_by
- 두 뷰는 delivery_id로 조인한다. 결과는 최대 {MAX_ROWS}행이므로 집계(GROUP BY)를 활용한다.
"""


TOOLS = [
    {"type": "function", "function": {
        "name": "get_kpi",
        "description": "대시보드와 동일한 KPI 집계(완료 건수, 로봇 이용률, 평균 배송시간, 적시 배송률, 성공률, 구간 평균, 집계 제외 건수). 기간 단위로 조회.",
        "parameters": {"type": "object", "properties": {
            "grain": {"type": "string", "enum": ["day", "week", "month", "custom"]},
            "anchor": {"type": "string", "description": "day/week/month에서 기준 날짜 YYYY-MM-DD (생략 시 기본: day=어제, week=이번 주, month=이번 달)"},
            "start": {"type": "string", "description": "custom 시작일 YYYY-MM-DD"},
            "end": {"type": "string", "description": "custom 종료일 YYYY-MM-DD"}},
            "required": ["grain"]}}},
    {"type": "function", "function": {
        "name": "query_sql",
        "description": "읽기 전용 PostgreSQL SELECT. ai_orders, ai_robot_notes 뷰만 조회 가능. 한 번에 한 문장.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
]


# ------------------------------------------------------------------ 질의
def _call_openai(messages: list) -> dict:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise AIError("OPENAI_API_KEY 가 .env에 설정되지 않았습니다")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o"
    r = requests.post(f"{base}/chat/completions", timeout=90,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto"})
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = r.text[:300]
        raise AIError(f"OpenAI 오류({r.status_code}): {msg}")
    return r.json()["choices"][0]["message"]


def ask(session, question: str, history: list | None = None) -> dict:
    question = (question or "").strip()
    if not question:
        raise AIError("질문을 입력하세요")
    messages = [{"role": "system", "content": system_prompt(session)}]
    for h in (history or [])[-8:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            messages.append({"role": h["role"], "content": h["content"][:4000]})
    messages.append({"role": "user", "content": question[:2000]})

    steps = []
    for _ in range(MAX_STEPS):
        msg = _call_openai(messages)
        calls = msg.get("tool_calls")
        if not calls:
            return {"answer": (msg.get("content") or "").strip(), "steps": steps}
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "get_kpi":
                result = tool_get_kpi(session, args.get("grain", "week"), args.get("anchor"),
                                      args.get("start"), args.get("end"))
            elif name == "query_sql":
                result = run_sql(session, args.get("sql", ""))
            else:
                result = {"error": f"알 수 없는 도구: {name}"}
            steps.append({"tool": name, "args": args,
                          "summary": result.get("error") or (
                              f"{result['row_count']}행" if "row_count" in result else "조회 완료")})
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    raise AIError("조회 단계가 너무 많아 중단했습니다. 질문을 더 구체적으로 바꿔 주세요")
