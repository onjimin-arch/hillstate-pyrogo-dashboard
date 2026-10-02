import os
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# Redash 쿼리 컬럼(한글) → raw_orders 컬럼. 원본 41컬럼 전체는 raw_json에 보관.
STR_COLS = {
    "배달ID": "delivery_id", "주문ID": "order_id", "로봇배송ID": "robot_delivery_id",
    "건물명": "building", "배송유형": "delivery_type", "상점구분": "store_type",
    "로봇매칭여부": "robot_matched", "로봇명": "robot_name", "상점명": "store_name",
    "주문처": "order_source", "라이더ID": "rider_id", "주문상태": "order_status",
    "배달상태": "delivery_status",
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
    cols = {c["name"] for c in inspect(engine).get_columns("robot_order_notes")}
    if "miss_reason" not in cols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE robot_order_notes ADD COLUMN miss_reason VARCHAR"))


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
