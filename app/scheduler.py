"""앱 안에서 주기적으로 Redash 수집을 돌리는 백그라운드 스레드(컨테이너 1개 전제).

- AUTO_COLLECT=0 이면 끈다. COLLECT_INTERVAL_MIN(기본 60분)마다 최근 N일 롤링 수집.
- 수집 이력이 한 번도 없으면 첫 실행은 전체 백필(월 단위)로 시작한다.
- 실패는 collect_log 에 남고 마지막 성공 데이터는 유지된다. 외부 스케줄러로 scripts/collect.py 를 돌린다면 끄면 된다.
- /admin/import 화면에서 상태 확인·수동 실행(trigger)을 할 수 있다.
"""
import logging
import os
import threading
from datetime import datetime

from sqlalchemy import func, select

from . import neubility
from .collector import CollectError, backfill_added_columns, orders_date_range, run_collect
from .collector import log as write_collect_log
from .db import CollectLog, get_session_factory

log = logging.getLogger("dashboard.scheduler")
_stop = threading.Event()
_run_lock = threading.Lock()          # 수집은 한 번에 하나만(주기 스레드·수동 실행 공용)
_trips_lock = threading.Lock()        # 시트 가져오기는 robot_trips 만 건드려 Redash 수집과 별도로 돈다
REDASH_ENV = ("REDASH_URL", "REDASH_API_KEY", "REDASH_QUERY_ID")
STATE = {"enabled": None, "interval_min": None, "running": False, "last_start": None,
         "last_error": None, "last_consent": None, "last_trips": None}


def _configured() -> bool:
    return all(os.getenv(k, "").strip() for k in REDASH_ENV)


def _never_collected() -> bool:
    with get_session_factory()() as s:
        return s.scalar(select(func.count()).select_from(CollectLog)
                        .where(CollectLog.status == "success")) == 0


def collect_once(force_full: bool = False) -> bool:
    """수집 1회. 이미 돌고 있으면 False. force_full=True 면 전체 백필(raw_orders 만 갱신)."""
    if not _run_lock.acquire(blocking=False):
        return False
    STATE.update(running=True, last_start=datetime.now().isoformat(timespec="seconds"), last_error=None)
    try:
        full = force_full or _never_collected()
        log.info("수집 시작 full=%s", full)
        n = run_collect(full=full)
        log.info("수집 완료 %s건", n)
    except CollectError as e:           # 이미 collect_log 에 기록됨
        STATE["last_error"] = str(e)[:300]
        log.error("수집 실패: %s", e)
    except Exception as e:              # noqa: BLE001 — 스레드는 죽지 않게 한다
        STATE["last_error"] = f"{type(e).__name__}: {str(e)[:250]}"
        log.exception("수집 오류")
    finally:
        STATE["running"] = False
        _run_lock.release()
    return True


def consent_backfill_once() -> bool:
    """이미 쌓인 주문 전 기간에 collector.FILL_COLS(동의상태·상점ID·결제방식)만 채운다.

    수집과 같은 락을 쓴다(동시 실행 금지).

    결과는 STATE["last_consent"] 로 /admin/import 화면에 보여주는데, 그건 프로세스 메모리라
    재배포하면 사라진다. 그래서 collect_log 에도 status='info' 로 한 줄 남긴다 — 'success' 로
    남기면 kpi.freshness 의 '마지막 수집 시각' 이 실제 수집과 무관하게 갱신되고, 'fail' 로
    남기면 화면이 '수집 실패' 로 보인다. info 는 둘 다 건드리지 않으면서 로그 표에는 남는다.
    """
    if not _run_lock.acquire(blocking=False):
        return False
    STATE.update(running=True, last_start=datetime.now().isoformat(timespec="seconds"), last_error=None)
    try:
        with get_session_factory()() as s:
            lo, hi = orders_date_range(s)
        if lo is None:
            STATE["last_consent"] = "raw_orders 가 비어 있어 건너뜀 — 먼저 수집을 돌려주세요"
            write_collect_log("info", 0, "보강 컬럼 백필: raw_orders 가 비어 있어 건너뜀")
            return True
        log.info("보강 컬럼 백필 시작 %s~%s", lo, hi)
        st = backfill_added_columns(lo, hi)
        msg = (f"{lo}~{hi} · Redash {st['fetched']:,}건 · 갱신 {st['updated']:,}건"
               f" · DB 에 없는 주문 {st['not_in_db']:,}건")
        for col, months in st["no_col"].items():
            msg += f" · ⚠ {col} 없는 달 {', '.join(months)} (쿼리 저장·QUERY_ID 확인)"
        if st["failed"]:
            msg += f" · 실패 {len(st['failed'])}개월"
        STATE["last_consent"] = msg
        write_collect_log("info", st["updated"], f"보강 컬럼 백필: {msg}")
        log.info("보강 컬럼 백필 완료 %s", msg)
    except CollectError as e:
        STATE["last_error"] = str(e)[:300]
        write_collect_log("info", 0, f"보강 컬럼 백필 실패: {str(e)[:200]}")
        log.error("보강 컬럼 백필 실패: %s", e)
    except Exception as e:              # noqa: BLE001 — 스레드는 죽지 않게 한다
        STATE["last_error"] = f"{type(e).__name__}: {str(e)[:250]}"
        log.exception("보강 컬럼 백필 오류")
    finally:
        STATE["running"] = False
        _run_lock.release()
    return True


def trips_once() -> bool:
    """공개 시트를 긁어 robot_trips 에 upsert. 이미 돌고 있으면 False.

    robot_trips 만 건드리므로 Redash 수집과 락을 나눠 쓴다(백필이 길어도 막히지 않는다).
    시트가 비공개로 바뀌거나 양식이 틀어지면 여기서 끝나고 기존 데이터는 그대로 남는다.
    collect_log 는 Redash 수집 전용이라 건드리지 않는다 — 남기면 '마지막 수집 시각'이 잘못 갱신된다.
    """
    if not _trips_lock.acquire(blocking=False):
        return False
    try:
        blob = neubility.fetch()
        with get_session_factory()() as s:
            trips, stat = neubility.parse(blob)
            n = neubility.upsert(s, trips, "auto-sheet")
        STATE["last_trips"] = (f"{datetime.now():%m-%d %H:%M} · {n:,}건 적재"
                               f" ({stat['date_from']}~{stat['date_to']})")
        log.info("시트 가져오기 완료 %s건", n)
    except neubility.SheetError as e:
        STATE["last_trips"] = f"{datetime.now():%m-%d %H:%M} · 실패: {str(e)[:200]}"
        log.error("시트 가져오기 실패: %s", e)
    except Exception as e:              # noqa: BLE001 — 스레드는 죽지 않게 한다
        STATE["last_trips"] = f"{datetime.now():%m-%d %H:%M} · 오류: {type(e).__name__}"
        log.exception("시트 가져오기 오류")
    finally:
        _trips_lock.release()
    return True


def trigger_trips() -> bool:
    """시트 가져오기를 백그라운드로 시작. 이미 진행 중이면 False."""
    if _trips_lock.locked():
        return False
    threading.Thread(target=trips_once, name="sheet-fetch", daemon=True).start()
    return True


def trigger_consent() -> bool:
    """동의상태 백필을 백그라운드로 시작. 수집이 진행 중이면 False."""
    if _run_lock.locked():
        return False
    threading.Thread(target=consent_backfill_once, name="consent-backfill", daemon=True).start()
    return True


def trigger(full: bool = False) -> bool:
    """수동 수집을 백그라운드로 시작. 이미 진행 중이면 False."""
    if _run_lock.locked():
        return False
    threading.Thread(target=collect_once, args=(full,), name="manual-collect", daemon=True).start()
    return True


def status() -> dict:
    # query_id 는 값 자체를 보여준다 — 비밀이 아니고(쿼리 URL 에 그대로 있다), 수집이 어느
    # 쿼리를 실행하는지 모르면 "컬럼이 안 온다" 를 화면에서 판단할 수 없다.
    return {**STATE, "configured": _configured(), "query_id": os.getenv("REDASH_QUERY_ID", "").strip() or None,
            "consent_query_id": os.getenv("REDASH_CONSENT_QUERY_ID", "").strip() or None,
            "sheet_configured": bool(neubility.sheet_url()),
            "env": {k: bool(os.getenv(k, "").strip()) for k in REDASH_ENV}}


def _loop(interval_sec: int) -> None:
    while not _stop.is_set():
        if _configured():
            collect_once()
        if neubility.sheet_url():
            trips_once()
        _stop.wait(interval_sec)


def start() -> None:
    STATE["enabled"] = False
    if os.getenv("AUTO_COLLECT", "1").strip() == "0":
        log.info("AUTO_COLLECT=0 — 자동 수집 꺼짐")
        return
    if not _configured() and not neubility.sheet_url():
        log.warning("REDASH_URL/REDASH_API_KEY/REDASH_QUERY_ID · NEUBILITY_SHEET_URL 미설정 — 자동 수집 안 함")
        return
    minutes = max(5, int(os.getenv("COLLECT_INTERVAL_MIN", "60") or 60))
    _stop.clear()
    STATE.update(enabled=True, interval_min=minutes)
    threading.Thread(target=_loop, args=(minutes * 60,), name="auto-collect", daemon=True).start()
    log.info("자동 수집 스레드 시작 (%d분 주기)", minutes)


def stop() -> None:
    _stop.set()
