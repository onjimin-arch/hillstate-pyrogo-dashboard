"""뉴빌리티 수기 기록(구글 시트 내보내기) → robot_trips.

로봇 복귀·가용률과 로봇 건 리스트의 동 표시에 쓴다(Redash 에 없는 두 가지).
수집 배치(Redash)와 무관하게 관리자 업로드로만 들어온다.

시트 특성상 흡수해야 하는 것:
- 시각이 HH:MM 분 단위다(초 없음). 그래서 우리 Redash 값과 수십 초씩 어긋난다 — 대조용이지 KPI 원본이 아니다.
- 날짜가 `YY.MM.DD` 이고 시각에는 날짜가 없다. 심야 배송은 자정을 넘기므로 앞 시각보다 이르면 하루 더한다.
- 헤더에 오타가 있다("적재함 딛힌 시각"). 열은 이름 완전일치가 아니라 조각 포함으로 찾는다.
- 호수(세대)는 읽지 않는다. 목적지에서 동만 떼어 쓴다(주문 ↔ 시트 짝짓기는 kpi.match_trips).
"""
import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime, time, timedelta

from .db import RobotTrip

log = logging.getLogger("dashboard.neubility")

DONE = "완료"
SKIP_STATUS = {"라이더_직배송"}      # 로봇을 안 탄 건 — 복귀·가용률 모수에서 제외


class SheetError(Exception):
    pass


def _find(hdr: list[str], *musts: str) -> int:
    """헤더에서 조각을 모두 포함하는 열을 찾는다. 오타·공백 변형을 견디려고 완전일치를 쓰지 않는다."""
    for i, h in enumerate(hdr):
        flat = re.sub(r"\s+", "", h or "")
        if all(re.sub(r"\s+", "", m) in flat for m in musts):
            return i
    raise SheetError(f"열을 찾을 수 없습니다: {' + '.join(musts)}")


def _close_col(hdr: list[str], where: str) -> int:
    """'적재함 닫힌/딛힌' 열. 시트 오타 때문에 '닫'·'딛' 둘 다 받는다."""
    for frag in ("닫힌", "딛힌", "닫힘"):
        try:
            return _find(hdr, where, "적재함", frag)
        except SheetError:
            continue
    raise SheetError(f"{where} 적재함 닫힘 열을 찾을 수 없습니다")


def _day(v: str) -> date | None:
    """`26.09.29` → 2026-09-29. `2026-09-29`, `2026.9.29` 도 받는다."""
    v = (v or "").strip()
    m = re.fullmatch(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", v)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _hhmm(v: str) -> time | None:
    v = (v or "").strip()
    if not v or v in ("-", "—"):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return time(h, mi) if h < 24 and mi < 60 else None


# 자정 넘김으로 볼 뒤집힘의 최소 폭. 23:55 → 00:05 는 23시간 50분 '거꾸로'로 보이므로
# 큰 뒤집힘만 하루를 더한다. 19:08 → 19:04 같은 몇 분짜리는 수기 입력 오류이지 자정이 아니다
# (그걸 자정으로 보정하면 1회전 점유가 24시간으로 튄다).
WRAP_MIN_BACK = timedelta(hours=12)


def _chain(day: date, times: list[time | None]) -> list[datetime | None]:
    """순서가 정해진 시각들을 datetime 으로. 자정을 넘긴 경우만 하루 더한다."""
    out: list[datetime | None] = []
    prev: datetime | None = None
    for t in times:
        if t is None:
            out.append(None)
            continue
        dt = datetime.combine(day, t)
        if prev is not None and dt < prev - WRAP_MIN_BACK:
            dt += timedelta(days=1)
        out.append(dt)
        prev = dt
    return out


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    """음수(시각이 거꾸로 적힌 수기 오류)는 값으로 치지 않는다 — 평균·중앙값을 망가뜨린다."""
    if a is None or b is None:
        return None
    d = (b - a).total_seconds()
    return d if d >= 0 else None


def _dong(v: str) -> str | None:
    """목적지에서 동만. 호수는 개별 세대라 떼어내고 버린다."""
    m = re.search(r"(\d+)\s*동", v or "")
    return m.group(1) if m else None


def _rows(blob: bytes) -> list[list[str]]:
    """CSV(UTF-8/CP949) 또는 XLSX."""
    if blob[:2] == b"PK":                       # xlsx
        import pandas as pd
        df = pd.read_excel(io.BytesIO(blob), dtype=str, header=None)
        return [["" if v is None or (isinstance(v, float)) else str(v) for v in r]
                for r in df.where(df.notna(), "").values.tolist()]
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            return list(csv.reader(io.StringIO(blob.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise SheetError("CSV 인코딩을 알 수 없습니다 (UTF-8 또는 CP949 로 저장해주세요)")


def parse(blob: bytes) -> tuple[list[RobotTrip], dict]:
    """(적재할 행, 요약). 요약은 업로드 결과 화면에 그대로 보여준다."""
    rows = _rows(blob)
    if not rows:
        raise SheetError("빈 파일입니다")
    hdr = rows[0]
    col = {
        "day": _find(hdr, "일자"), "status": _find(hdr, "상태"),
        "robot": _find(hdr, "로봇", "ID"), "dest": _find(hdr, "목적지"),
        "dock_close": _close_col(hdr, "도킹존"), "unit_close": _close_col(hdr, "세대"),
        "ret_start": _find(hdr, "복귀시작"), "ret_done": _find(hdr, "복귀완료"),
    }
    try:
        col["note"] = _find(hdr, "비고")
    except SheetError:
        col["note"] = None

    out, seen = [], set()
    stat = {"total": 0, "parsed": 0, "skipped_status": 0, "skipped_empty": 0,
            "skipped_no_time": 0, "duplicated": 0, "time_reversed": 0}
    for r in rows[1:]:
        def g(k):
            i = col[k]
            return (r[i].strip() if i is not None and i < len(r) else "")

        if not any(x.strip() for x in r):
            continue
        stat["total"] += 1
        day, status = _day(g("day")), g("status")
        if day is None or not status:
            stat["skipped_empty"] += 1          # 시트 하단의 합계·빈 행
            continue
        if status in SKIP_STATUS:
            stat["skipped_status"] += 1         # 라이더 직배송 — 로봇이 안 탔다
            continue

        dock, fin, rs, rd = _chain(day, [_hhmm(g("dock_close")), _hhmm(g("unit_close")),
                                         _hhmm(g("ret_start")), _hhmm(g("ret_done"))])
        if dock is None and fin is None:
            stat["skipped_no_time"] += 1        # 수동주행 등 시각이 '-' 인 건
            continue

        key_src = f"{day}|{g('robot')}|{g('dock_close')}|{g('unit_close')}"
        trip_key = hashlib.sha1(key_src.encode()).hexdigest()[:20]
        if trip_key in seen:
            stat["duplicated"] += 1
            continue
        seen.add(trip_key)

        segs = (_secs(dock, fin), _secs(fin, rs), _secs(rs, rd), _secs(dock, rd))
        if any(v is None for v in segs) and all(x is not None for x in (dock, fin, rs, rd)):
            stat["time_reversed"] += 1          # 시각은 다 있는데 순서가 뒤집힌 건

        out.append(RobotTrip(
            trip_key=trip_key, ord_date=day, robot_id=g("robot") or None, status=status,
            dong=_dong(g("dest")),
            dock_close_dt=dock, robot_finish_dt=fin, return_start_dt=rs, return_done_dt=rd,
            s_dockclose_finish=segs[0], s_finish_return_start=segs[1],
            s_return=segs[2], s_cycle=segs[3],
            note=(g("note") or None) if col["note"] is not None else None))
        stat["parsed"] += 1

    if not out:
        raise SheetError("적재할 행이 없습니다. 열 이름과 일자 형식을 확인해주세요.")
    stat["date_from"] = min(t.ord_date for t in out)
    stat["date_to"] = max(t.ord_date for t in out)
    return out, stat


def upsert(session, trips: list[RobotTrip], who: str | None) -> int:
    """같은 건(trip_key)은 덮어쓴다 — 뉴빌리티가 시트를 고쳐 다시 올리는 경우가 있다."""
    now = datetime.now()
    for t in trips:
        t.uploaded_at, t.uploaded_by = now, who
        session.merge(t)
    session.commit()
    log.info("robot_trips upsert rows=%d by=%s", len(trips), who)
    return len(trips)
