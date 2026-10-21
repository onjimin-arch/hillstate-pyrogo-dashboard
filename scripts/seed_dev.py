"""로컬 화면 확인용 SQLite 시드. 실데이터·실상점명은 쓰지 않는다(전부 가상 값).

    python scripts/seed_dev.py            # data/dev.db 새로 만들기
    python scripts/seed_dev.py --keep     # 기존 입력(사유·특이사항) 보존하고 주문만 다시 깔기

구간 표시가 맞는지 눈으로 보려고 만든 것이므로, 화면에서 갈리는 경우를 일부러 다 넣는다.
"""
import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "dev.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB_PATH.as_posix()}")

from app import db as dbm  # noqa: E402
from app.config import CFG  # noqa: E402

# 가상 상점/주문처 (실제 상호 금지)
SOURCES = ["가상주문처A", "가상주문처B", "가상주문처C"]
STORES = ["가상상점 1호", "가상상점 2호", "가상상점 3호", "가상상점 4호"]
ROBOTS = ["가상로봇 1호기", "가상로봇 2호기", "가상로봇 3호기"]


def mk(i, ord_dt, robot, store_type, s1, s2, s3, s4, tail, *,
       status="DROP_FINISHED", dispatch_count=1, consent="동의", drop=()):
    """구간 초를 받아 타임스탬프까지 역산해 넣는다. drop 에 적은 필드는 비운다(결측 재현)."""
    dispatch_dt = ord_dt + timedelta(seconds=s1)
    pickup_dt = dispatch_dt + timedelta(seconds=s2)
    dock_close_dt = pickup_dt + timedelta(seconds=s3) if s3 is not None else None
    robot_finish_dt = dock_close_dt + timedelta(seconds=s4) if s4 is not None else None
    last = robot_finish_dt if robot else pickup_dt
    finish_dt = last + timedelta(seconds=tail) if (last and tail is not None) else None
    total = (finish_dt - ord_dt).total_seconds() if finish_dt else None

    o = dbm.RawOrder(
        delivery_id=f"D{i:04d}", order_id=f"O{i:04d}",
        robot_delivery_id=f"R{i:04d}" if robot else None,
        building=CFG["building"], delivery_type="로봇연계" if robot else "일반",
        store_type=store_type, robot_matched="Y" if robot else "N",
        robot_name=random.choice(ROBOTS) if robot else None,
        store_name=random.choice(STORES), store_id=f"S{i % 4 + 1:03d}",
        store_robot_consent=consent, order_source=random.choice(SOURCES),
        rider_id=f"RIDER{i % 7 + 1:03d}", order_status="DONE",
        delivery_status=status, dispatch_count=dispatch_count,
        ord_dt=ord_dt, dispatch_dt=dispatch_dt, pickup_dt=pickup_dt,
        dock_close_dt=dock_close_dt, robot_finish_dt=robot_finish_dt, finish_dt=finish_dt,
        s_order_dispatch=s1, s_dispatch_pickup=s2,
        s_order_pickup=(s1 + s2) if (s1 is not None and s2 is not None) else None,
        s_pickup_dockclose=s3, s_dockclose_robotfinish=s4, s_order_finish=total,
        collected_at=datetime.now())
    for f in drop:                       # 결측 케이스: 해당 필드를 비워 "-" 표시를 확인한다
        setattr(o, f, None)
    return o


def pick(core, tail, p_tail):
    """현실적인 분포: 대부분 core 범위, 일부(p_tail)만 tail 범위로 길게 샌다.
    균등 랜덤으로 깔면 '긴 건'이 절반이 되어 이상치 강조를 눈으로 볼 수 없다."""
    lo, hi = tail if random.random() < p_tail else core
    return random.randint(lo, hi)


def build(base: date):
    """base 포함 최근 14일. 매일 로봇 3~6건 + 일반 6~12건."""
    random.seed(20261014)               # 돌릴 때마다 같은 화면이 나오게 고정
    out, i = [], 0
    for back in range(14):
        day = base - timedelta(days=back)
        for _ in range(random.randint(3, 6)):            # 로봇 건
            i += 1
            h, m = random.randint(11, 21), random.randint(0, 59)
            out.append(mk(i, datetime(day.year, day.month, day.day, h, m),
                          True, "일반(로드샵)",
                          s1=pick((15, 70), (180, 420), 0.08),
                          s2=pick((420, 900), (1500, 2600), 0.15),   # 조리 대기 — 가끔 크게 샌다
                          s3=pick((150, 360), (600, 900), 0.08),
                          s4=pick((300, 430), (520, 780), 0.25),     # 420초 초과면 ▲
                          tail=random.choice([-1, -2, 0, 1])))   # 로봇 건은 사실상 '동시'
        for _ in range(random.randint(6, 12)):           # 일반 건
            i += 1
            h, m = random.randint(11, 22), random.randint(0, 59)
            out.append(mk(i, datetime(day.year, day.month, day.day, h, m),
                          False, random.choice(["일반(로드샵)", "일반(로드샵)", "B2B"]),
                          s1=pick((15, 90), (240, 600), 0.08),
                          s2=pick((420, 840), (1400, 2400), 0.12),
                          s3=None, s4=None,
                          tail=pick((400, 800), (1300, 2100), 0.10),  # 일반은 ⑤가 라이더 배송 구간
                          consent=random.choice(["동의", "미동의", "미응답"]),
                          dispatch_count=random.choice([1, 1, 1, 2, 3])))

    # 화면에서 갈리는 특수 케이스들
    d = base
    i += 1; out.append(mk(i, datetime(d.year, d.month, d.day, 17, 33), True, "일반(로드샵)",
                          s1=12, s2=2052, s3=330, s4=426, tail=-1))      # ②가 최장인 긴 건
    i += 1; out.append(mk(i, datetime(d.year, d.month, d.day, 18, 32), True, "일반(로드샵)",
                          s1=12, s2=504, s3=198, s4=552, tail=-2))       # ④가 최장(▲)
    i += 1; out.append(mk(i, datetime(d.year, d.month, d.day, 19, 10), True, "일반(로드샵)",
                          s1=30, s2=600, s3=240, s4=300, tail=240))      # ⑤가 '동시'가 아닌 로봇 건
    i += 1; out.append(mk(i, datetime(d.year, d.month, d.day, 19, 40), True, "일반(로드샵)",
                          s1=30, s2=600, s3=240, s4=300, tail=0,
                          drop=("robot_finish_dt",)))                    # ⑤ 결측("-")
    i += 1; out.append(mk(i, datetime(d.year, d.month, d.day, 20, 5), False, "일반(로드샵)",
                          s1=45, s2=900, s3=None, s4=None, tail=600,
                          dispatch_count=4))                             # 재배차 표식
    i += 1; out.append(mk(i, datetime(d.year, d.month, d.day, 20, 30), False, "B2B",
                          s1=60, s2=1200, s3=None, s4=None, tail=None,
                          status="CANCELED"))                            # 취소 건
    return out


# 가상 동 번호(실제 단지 동 번호 아님)
DONGS = ["901", "902", "903", "904"]


def robot_trips(orders):
    """뉴빌리티 시트 업로드분(가상). 로봇 건 리스트의 '동' 과 복귀·가용률을 눈으로 보려는 것.

    실제 시트처럼 분 단위로 자르고(초 버림) 일부 건은 일부러 빼 둔다 — 시트에 없는 건이
    '-' 로 나오는지 확인해야 한다."""
    import hashlib
    rows = []
    robots = [o for o in orders if o.delivery_type == "로봇연계" and o.robot_finish_dt]
    for i, o in enumerate(robots):
        if i % 7 == 0:                 # 7건에 1건은 시트에 없음 → 화면에서 '-'
            continue
        dock = o.dock_close_dt.replace(second=0, microsecond=0)
        fin = o.robot_finish_dt.replace(second=0, microsecond=0)
        rs = fin + timedelta(seconds=random.randint(30, 240))
        rd = rs + timedelta(seconds=random.randint(120, 420))
        rs, rd = rs.replace(second=0), rd.replace(second=0)
        rows.append(dbm.RobotTrip(
            trip_key=hashlib.sha1(f"{o.delivery_id}".encode()).hexdigest()[:20],
            ord_date=dock.date(), robot_id=_robot_num(o.robot_name), status="완료",
            dong=random.choice(DONGS),
            dock_close_dt=dock, robot_finish_dt=fin, return_start_dt=rs, return_done_dt=rd,
            s_dockclose_finish=(fin - dock).total_seconds(),
            s_finish_return_start=(rs - fin).total_seconds(),
            s_return=(rd - rs).total_seconds(), s_cycle=(rd - dock).total_seconds(),
            note=None, uploaded_at=datetime.now(), uploaded_by="seed"))
    return rows


def _robot_num(name):
    import re
    nums = re.findall(r"\d+", name or "")
    return nums[-1] if nums else None


def store_consent(base: date):
    """상점 동의 명부(가상). 주문이 없는 상점도 섞어 둔다 — 명부 기준 모수가 주문 기준보다
    넓다는 걸 화면에서 확인하려는 것이다."""
    rows, states = [], ["동의", "동의", "미동의", "미응답"]
    for i in range(1, 13):                       # S001~S004 는 주문에도 쓰이는 상점
        st = states[i % 4]
        rows.append(dbm.StoreConsent(
            store_id=f"S{i:03d}", store_name=f"가상상점 {i}호",
            store_type="일반(로드샵)" if i % 5 else "B2B", manage_agency="self",
            consent=st,
            first_response_date=base - timedelta(days=20 + i) if st != "미응답" else None,
            agreed_at=None, last_changed_date=base - timedelta(days=i) if st != "미응답" else None,
            changed_after_response="Y" if i % 6 == 0 else "N",
            collected_at=datetime.now()))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="기존 DB 유지(사용자 입력 보존)")
    ap.add_argument("--date", default=None, help="기준일 YYYY-MM-DD (기본: 오늘)")
    a = ap.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists() and not a.keep:
        try:
            DB_PATH.unlink()
        except PermissionError:
            raise SystemExit("dev.db 를 다른 프로세스가 쓰고 있습니다. "
                             "scripts/dev.py 를 먼저 끄고(Ctrl+C) 다시 실행하세요.")

    base = date.fromisoformat(a.date) if a.date else date.today()
    SL = dbm.make_session_factory()
    orders = build(base)
    with SL() as s:
        for o in orders:
            s.merge(o)
        for r in store_consent(base):
            s.merge(r)
        for t in robot_trips(orders):
            s.merge(t)
        s.commit()
        n = s.query(dbm.RawOrder).count()
        nt = s.query(dbm.RobotTrip).count()
    print(f"{DB_PATH} · 주문 {n}건 · 시트(뉴빌리티) {nt}건 ({base - timedelta(days=13)} ~ {base})")
    print(f'DATABASE_URL=sqlite:///{DB_PATH.as_posix()}')


if __name__ == "__main__":
    main()
