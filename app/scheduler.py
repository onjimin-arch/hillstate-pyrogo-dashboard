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

from .collector import CollectError, backfill_consent, orders_date_range, run_collect
from .db import CollectLog, get_session_factory

log = logging.getLogger("dashboard.scheduler")
_stop = threading.Event()
_run_lock = threading.Lock()          # 수집은 한 번에 하나만(주기 스레드·수동 실행 공용)
REDASH_ENV = ("REDASH_URL", "REDASH_API_KEY", "REDASH_QUERY_ID")
STATE = {"enabled": None, "interval_min": None, "running": False, "last_start": None,
         "last_error": None, "last_consent": None}


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
    """이미 쌓인 주문 전 기간에 상점로봇동의상태만 채운다. 수집과 같은 락을 쓴다(동시 실행 금지).

    collect_log 에는 남기지 않는다 — 수집이 아니므로 '마지막 수집 시각'을 움직이면 안 된다.
    결과는 STATE["last_consent"] 로 /admin/import 화면에 보여준다.
    """
    if not _run_lock.acquire(blocking=False):
        return False
    STATE.update(running=True, last_start=datetime.now().isoformat(timespec="seconds"), last_error=None)
    try:
        with get_session_factory()() as s:
            lo, hi = orders_date_range(s)
        if lo is None:
            STATE["last_consent"] = "raw_orders 가 비어 있어 건너뜀 — 먼저 수집을 돌려주세요"
            return True
        log.info("동의상태 백필 시작 %s~%s", lo, hi)
        st = backfill_consent(lo, hi)
        msg = (f"{lo}~{hi} · Redash {st['fetched']:,}건 · 갱신 {st['updated']:,}건"
               f" · DB 에 없는 주문 {st['not_in_db']:,}건")
        if st["no_consent_col"]:
            msg += f" · ⚠ 동의 컬럼 없는 달 {', '.join(st['no_consent_col'])} (쿼리 저장·QUERY_ID 확인)"
        if st["failed"]:
            msg += f" · 실패 {len(st['failed'])}개월"
        STATE["last_consent"] = msg
        log.info("동의상태 백필 완료 %s", msg)
    except CollectError as e:
        STATE["last_error"] = str(e)[:300]
        log.error("동의상태 백필 실패: %s", e)
    except Exception as e:              # noqa: BLE001 — 스레드는 죽지 않게 한다
        STATE["last_error"] = f"{type(e).__name__}: {str(e)[:250]}"
        log.exception("동의상태 백필 오류")
    finally:
        STATE["running"] = False
        _run_lock.release()
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
    return {**STATE, "configured": _configured(), "env": {k: bool(os.getenv(k, "").strip()) for k in REDASH_ENV}}


def _loop(interval_sec: int) -> None:
    while not _stop.is_set():
        collect_once()
        _stop.wait(interval_sec)


def start() -> None:
    STATE["enabled"] = False
    if os.getenv("AUTO_COLLECT", "1").strip() == "0":
        log.info("AUTO_COLLECT=0 — 자동 수집 꺼짐")
        return
    if not _configured():
        log.warning("REDASH_URL/REDASH_API_KEY/REDASH_QUERY_ID 미설정 — 자동 수집 안 함")
        return
    minutes = max(5, int(os.getenv("COLLECT_INTERVAL_MIN", "60") or 60))
    _stop.clear()
    STATE.update(enabled=True, interval_min=minutes)
    threading.Thread(target=_loop, args=(minutes * 60,), name="auto-collect", daemon=True).start()
    log.info("자동 수집 스레드 시작 (%d분 주기)", minutes)


def stop() -> None:
    _stop.set()
