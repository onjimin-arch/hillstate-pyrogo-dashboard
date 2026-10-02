import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_kpi import make_order, session  # noqa: E402,F401  (fixture 재사용)

from app import ai  # noqa: E402
from app import db as dbm  # noqa: E402


@pytest.fixture()
def s(session):  # noqa: F811
    ai.ensure_views(session)
    return session


def test_select_allowed(s):
    r = ai.run_sql(s, "SELECT delivery_type, COUNT(*) n FROM ai_orders WHERE in_kpi_scope=1 GROUP BY 1 ORDER BY 1")
    assert r["rows"] == [["로봇연계", 3], ["일반", 2]]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM raw_orders",                                   # 원본 테이블 직접 접근
    "SELECT raw_json FROM ai_orders",                             # 뷰에 없는 컬럼
    "SELECT rider_id FROM ai_orders",
    "DELETE FROM robot_order_notes",
    "UPDATE raw_orders SET building='x'",
    "SELECT 1; DROP TABLE raw_orders",
    "PRAGMA table_info(raw_orders)",
    "ATTACH DATABASE 'x.db' AS x",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM collect_log",
])
def test_sql_guard_blocks(s, sql):
    r = ai.run_sql(s, sql)
    assert "error" in r, sql
    # 차단 이후에도 데이터가 그대로이고 연결이 정상 복구된다
    assert ai.run_sql(s, "SELECT COUNT(*) FROM ai_orders")["rows"][0][0] == 7


def test_notes_view_readable(s):
    s.add(dbm.RobotOrderNote(delivery_id="4", result_type="실패", note="적재함 오류"))
    s.commit()
    r = ai.run_sql(s, "SELECT o.delivery_id, n.note FROM ai_orders o JOIN ai_robot_notes n USING(delivery_id)")
    assert r["rows"] == [["4", "적재함 오류"]]


def test_get_kpi_matches_dashboard(s):
    r = ai.tool_get_kpi(s, "day", "2026-09-30")
    assert r["metrics"]["completed"] == 5 and r["metrics"]["timely_base"] == 3
    assert r["period"]["data_through"] == "2026-09-30"


def test_ask_tool_loop(s, monkeypatch):
    replies = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "get_kpi", "arguments": json.dumps({"grain": "day", "anchor": "2026-09-30"})}}]},
        {"role": "assistant", "content": "로봇 적시 배송률은 33.3%(1/3건)입니다."},
    ]
    seen = []

    def fake(messages):
        seen.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(ai, "_call_openai", fake)
    out = ai.ask(s, "어제 적시 배송률?")
    assert "33.3%" in out["answer"] and out["steps"][0]["tool"] == "get_kpi"
    tool_msg = seen[1][-1]
    assert tool_msg["role"] == "tool" and "timely_pct" in tool_msg["content"]
    assert "rider" not in seen[0][0]["content"].split("## query_sql")[1]   # 프롬프트에 라이더ID 컬럼 노출 없음


def test_missing_key(s, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ai.AIError):
        ai.ask(s, "안녕")


def test_ai_sees_notes_and_exclusion(s):
    s.add(dbm.RobotOrderNote(delivery_id="5", result_type="기타", note="라이더 직접 배송", exclude_from_kpi=True))
    s.commit()
    r = ai.run_sql(s, "SELECT delivery_id, note, exclude_from_kpi, in_kpi_scope FROM ai_orders WHERE note IS NOT NULL")
    assert r["rows"] == [["5", "라이더 직접 배송", 1, 0]]
    assert ai.run_sql(s, "SELECT SUM(in_kpi_scope) FROM ai_orders")["rows"][0][0] == 4
    k = ai.tool_get_kpi(s, "day", "2026-09-30")
    assert k["metrics"]["completed"] == 4 and k["metrics"]["excluded_n"] == 1
    assert k["noted_robot_orders"][0]["note"] == "라이더 직접 배송" and k["noted_robot_orders"][0]["excluded"]


def test_ai_time_scope_excludes_error_completion(s):
    s.add(dbm.RobotOrderNote(delivery_id="5", result_type="완료(오류)"))
    s.commit()
    r = ai.run_sql(s, "SELECT SUM(in_kpi_scope), SUM(in_time_scope) FROM ai_orders")
    assert r["rows"][0] == [5, 4]
