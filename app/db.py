import os
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# Redash 쿼리 컬럼(한글) → raw_orders 컬럼. 원본 41컬럼 전체는 raw_json에 보관.
STR_COLS = {
    "배달ID": "delivery_id", "주문ID": "order_id", "로봇배송ID": "robot_delivery_id",
    "건물명": "building", "배송유형": "delivery_type", "상점구분": "store_type",
    "로봇매칭여부": "robot_matched", "로봇명": "robot_name", "상점명": "store_name",
    "주문처": "order_source", "라이더ID": "rider_id", "주문상태": "order_status",
    "배달상태": "delivery_status",
    "상점ID": "store_id", "상점로봇동의상태": "store_robot_consent",   # 동의/미동의/미응답 (Redash 쿼리에서 산출)
}
DT_COLS = {
    "상점주문접수일시": "ord_dt", "라이더배차일시": "dispatch_dt",
    "라이더픽업완료일시": "pickup_dt", "도킹존_적재함닫힘일시": "dock_close_dt",
    "로봇배송완료일시": "robot_finish_dt", "배달완료일시": "finish_dt",
}
SEC_COLS = {
    "주문접수_배차_초": "s_order_dispatch", "배차_픽업완료_초": "s_dispatch_pickup",
    "주문접수_픽업완료_초": "s_order_pickup", "픽업완료_적재함닫힘_초": "s_pickup_dockclose",
    "적재함닫힘_로봇배송완료_초": "s_dockclose_robotfinish", "주문접수_배달완료_초": "s_order_finish",
}
REQUIRED_COLS = ["배달ID", "건물명", "배송유형", "상점구분", "배달상태", "상점주문접수일시",
                 "주문접수_배달완료_초"]
# 수집이 읽는 전체 컬럼. 필수가 아닌 것은 없어도 수집이 돌지만 그만큼 조용히 NULL 이 된다
# (상점로봇동의상태가 빠진 채 수집돼 화면이 전부 "-" 로 나온 적이 있다). 누락은 수집 로그에 남긴다.
ALL_COLS = [*STR_COLS, *DT_COLS, *SEC_COLS]

# 상점 동의 현황 쿼리(REDASH_CONSENT_QUERY_ID) 컬럼 → store_consent. 주문 쿼리와 다른 쿼리다.
# 모수가 '수원시 승인 상점 전체 + 이 건물에 주문이 있던 상점' 이라 주문이 없는 상점도 들어온다.
CONSENT_STR_COLS = {
    "storeId": "store_id", "name": "store_name", "상점구분": "store_type",
    "관리주체": "manage_agency", "동의여부": "consent", "응답후변경": "changed_after_response",
}
CONSENT_DATE_COLS = {"최초응답일": "first_response_date", "최종변경일": "last_changed_date"}
CONSENT_DT_COLS = {"동의일시": "agreed_at"}
CONSENT_REQUIRED_COLS = ["storeId", "동의여부"]
# 주문건수는 받아도 저장하지 않는다 — 조회 기간에 따라 달라지는 값이라 테이블에 두면
# 어느 기간 기준인지 알 수 없게 된다. 기간별 건수는 raw_orders 에서 센다.
ALL_CONSENT_COLS = [*CONSENT_STR_COLS, *CONSENT_DATE_COLS, *CONSENT_DT_COLS]


class Base(DeclarativeBase):
    pass


class RawOrder(Base):
    """수집 배치만 쓰는 테이블."""
    __tablename__ = "raw_orders"
    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String)
    robot_delivery_id: Mapped[str | None] = mapped_column(String)
    building: Mapped[str | None] = mapped_column(String)
    delivery_type: Mapped[str | None] = mapped_column(String)
    store_type: Mapped[str | None] = mapped_column(String)
    robot_matched: Mapped[str | None] = mapped_column(String)
    robot_name: Mapped[str | None] = mapped_column(String)
    store_name: Mapped[str | None] = mapped_column(String)
    store_id: Mapped[str | None] = mapped_column(String)
    store_robot_consent: Mapped[str | None] = mapped_column(String)   # 동의/미동의/미응답 (NULL=컬럼 추가 전 수집분)
    order_source: Mapped[str | None] = mapped_column(String)
    rider_id: Mapped[str | None] = mapped_column(String)
    order_status: Mapped[str | None] = mapped_column(String)
    delivery_status: Mapped[str | None] = mapped_column(String)
    dispatch_count: Mapped[int | None] = mapped_column(Integer)
    ord_dt: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    dispatch_dt: Mapped[datetime | None] = mapped_column(DateTime)
    pickup_dt: Mapped[datetime | None] = mapped_column(DateTime)
    dock_close_dt: Mapped[datetime | None] = mapped_column(DateTime)
    robot_finish_dt: Mapped[datetime | None] = mapped_column(DateTime)
    finish_dt: Mapped[datetime | None] = mapped_column(DateTime)
    s_order_dispatch: Mapped[float | None] = mapped_column(Float)
    s_dispatch_pickup: Mapped[float | None] = mapped_column(Float)
    s_order_pickup: Mapped[float | None] = mapped_column(Float)
    s_pickup_dockclose: Mapped[float | None] = mapped_column(Float)
    s_dockclose_robotfinish: Mapped[float | None] = mapped_column(Float)
    s_order_finish: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime)


class RobotOrderNote(Base):
    """사용자 입력 전용(웹앱만 쓰기). 수집 upsert로 덮어쓰지 않는다."""
    __tablename__ = "robot_order_notes"
    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    result_type: Mapped[str | None] = mapped_column(String)   # 정상/실패/기타 (None=정상 간주)
    miss_reason: Mapped[str | None] = mapped_column(String)   # 로봇 누락 사유 (일반 주문용)
    note: Mapped[str | None] = mapped_column(Text)
    exclude_from_kpi: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_by: Mapped[str | None] = mapped_column(String)


class RobotTrip(Base):
    """뉴빌리티 수기 기록(구글 시트) 업로드분. 로봇 복귀·가용률 산출 전용.

    호수(세대)는 저장하지 않는다 — 개별 배송지를 특정하는 값이라 수집 대상이 아니다.
    동은 구역 단위라 보관한다. 로봇 건 리스트에 한 건씩 표시하지만(kpi.dong_map) 집계에는
    쓰지 않는다 — 표본이 동당 2건 수준이라 동별 평균은 의미가 없다.
    수집 배치가 아니라 관리자 업로드로만 들어온다.
    """
    __tablename__ = "robot_trips"
    trip_key: Mapped[str] = mapped_column(String, primary_key=True)   # 일자|로봇ID|적재함닫힘시각
    ord_date: Mapped[date | None] = mapped_column(Date, index=True)
    robot_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)                # 완료 / 라이더_직배송
    dong: Mapped[str | None] = mapped_column(String)                  # 동만. 호수는 받지 않는다
    dock_close_dt: Mapped[datetime | None] = mapped_column(DateTime)  # (도킹존) 적재함 닫힘
    robot_finish_dt: Mapped[datetime | None] = mapped_column(DateTime)  # (세대 앞) 적재함 닫힘 = 로봇배송완료
    return_start_dt: Mapped[datetime | None] = mapped_column(DateTime)
    return_done_dt: Mapped[datetime | None] = mapped_column(DateTime)
    s_dockclose_finish: Mapped[float | None] = mapped_column(Float)   # 우리 ④와 같은 구간(대조용)
    s_finish_return_start: Mapped[float | None] = mapped_column(Float)  # 배송완료→복귀시작(대기)
    s_return: Mapped[float | None] = mapped_column(Float)             # 복귀시작→복귀완료
    s_cycle: Mapped[float | None] = mapped_column(Float)              # 적재함닫힘→복귀완료(1회전 점유)
    note: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    uploaded_by: Mapped[str | None] = mapped_column(String)


class StoreConsent(Base):
    """상점별 로봇배송 동의 현황(상점 단위 명부). 주문 수집과 다른 Redash 쿼리에서 온다.

    raw_orders.store_robot_consent 는 '주문이 있었던 상점'만 커버하지만 여기는 주문이 없는
    상점도 들어온다 — 동의는 했는데 아직 주문이 없는 상점이 모수에서 빠지지 않게 하려는 것.
    상태는 현재값이고, 과거 시점 판단은 last_changed_date 로 한다(주문일보다 이르면 그때도 같은 상태).
    """
    __tablename__ = "store_consent"
    store_id: Mapped[str] = mapped_column(String, primary_key=True)
    store_name: Mapped[str | None] = mapped_column(String)
    store_type: Mapped[str | None] = mapped_column(String)          # 일반(로드샵) / B2B
    manage_agency: Mapped[str | None] = mapped_column(String)
    consent: Mapped[str | None] = mapped_column(String)             # 동의 / 미동의 / 미응답
    first_response_date: Mapped[date | None] = mapped_column(Date)
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_changed_date: Mapped[date | None] = mapped_column(Date)
    changed_after_response: Mapped[str | None] = mapped_column(String)   # Y / N
    collected_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppUser(Base):
    """접속 허용 사용자(등록된 이메일만 접속 가능). role: admin / user. 기본 관리자는 코드(BOOTSTRAP_ADMINS)에 고정."""
    __tablename__ = "app_users"
    email: Mapped[str] = mapped_column(String, primary_key=True)   # 소문자
    role: Mapped[str] = mapped_column(String, default="user")
    name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str | None] = mapped_column(String)


class CollectLog(Base):
    __tablename__ = "collect_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String)   # success / fail
    rows: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)


def _migrate(engine) -> None:
    """기존 DB에 신규 컬럼 추가 (사용자 입력 데이터 보존)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("robot_order_notes")}
    if "miss_reason" not in cols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE robot_order_notes ADD COLUMN miss_reason VARCHAR"))
    raw_cols = {c["name"] for c in insp.get_columns("raw_orders")}
    for name in ("store_id", "store_robot_consent"):
        if name not in raw_cols:
            with engine.begin() as c:
                c.execute(text(f"ALTER TABLE raw_orders ADD COLUMN {name} VARCHAR"))


def database_url() -> str:
    """접속 정보는 환경변수 DATABASE_URL 에서만 읽는다."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("환경변수 DATABASE_URL 이 설정되지 않았습니다")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def make_session_factory(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite"):      # 단위 테스트용
        extra = {"poolclass": StaticPool} if url in ("sqlite://", "sqlite:///:memory:") else {}
        engine = create_engine(url, connect_args={"check_same_thread": False}, **extra)
    else:
        engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(engine)    # CREATE TABLE IF NOT EXISTS 와 동일, 재실행 안전
    _migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


SessionLocal = None


def get_session_factory():
    global SessionLocal
    if SessionLocal is None:
        SessionLocal = make_session_factory()
    return SessionLocal
