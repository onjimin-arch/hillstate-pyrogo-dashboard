"""앱 안에서 주기적으로 Redash 수집을 돌리는 백그라운드 스레드(컨테이너 1개 전제).

- AUTO_COLLECT=0 이면 끈다. COLLECT_INTERVAL_MIN(기본 60분)마다 최근 N일 롤링 수집.
- 수집 이력이 한 번도 없으면 첫 실행은 전체 백필(월 단위)로 시작한다.
- 실패는 collect_log 에 남고 마지막 성공 데이터는 유지된다. 외부 스케줄러로 scripts/collect.py 를 돌린다면 끄면 된다.
"""
import logging
import os
import threading

from sqlalchemy import func, select

from .collector import CollectError, run_collect
from .db import CollectLog, get_session_factory

log = logging.getLogger("dashboard.scheduler")
_stop = threading.Event()


def _configured() -> bool:
    return all(os.getenv(k, "").strip() for k in ("REDASH_URL", "REDASH_API_KEY", "REDASH_QUERY_ID"))


def _never_collected() -> bool:
    with get_session_factory()() as s:
        return s.scalar(select(func.count()).select_from(CollectLog)
                        .where(CollectLog.status == "success")) == 0


def _loop(interval_sec: int) -> None:
    while not _stop.is_set():
        try:
            full = _never_collected()
            log.info("자동 수집 시작 full=%s", full)
            n = run_collect(full=full)
            log.info("자동 수집 완료 %s건", n)
        except CollectError as e:           # 이미 collect_log 에 기록됨
            log.error("자동 수집 실패: %s", e)
        except Exception:                   # noqa: BLE001 — 스레드는 죽지 않게 한다
            log.exception("자동 수집 오류")
        _stop.wait(interval_sec)


def start() -> None:
    if os.getenv("AUTO_COLLECT", "1").strip() == "0":
        log.info("AUTO_COLLECT=0 — 자동 수집 꺼짐")
        return
    if not _configured():
        log.warning("REDASH_URL/REDASH_API_KEY/REDASH_QUERY_ID 미설정 — 자동 수집 안 함")
        return
    minutes = max(5, int(os.getenv("COLLECT_INTERVAL_MIN", "60") or 60))
    _stop.clear()
    threading.Thread(target=_loop, args=(minutes * 60,), name="auto-collect", daemon=True).start()
    log.info("자동 수집 스레드 시작 (%d분 주기)", minutes)


def stop() -> None:
    _stop.set()
