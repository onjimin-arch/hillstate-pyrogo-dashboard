"""로컬 개발 서버. 배포 없이 화면을 바로 보려고 쓴다.

    python scripts/seed_dev.py     # 처음 한 번 (data/dev.db 생성)
    python scripts/dev.py          # http://127.0.0.1:8000

app/*.py 를 고치면 서버가 자동으로 다시 뜨고, 템플릿(.html)은 새로고침만 하면 된다.
app.css 는 브라우저 캐시 때문에 강력 새로고침(Ctrl+F5)이 필요할 수 있다.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "dev.db"       # data/ 는 .gitignore 에 있다

if not DB.exists():
    raise SystemExit("data/dev.db 가 없습니다. 먼저 실행하세요:  python scripts/seed_dev.py")

os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB.as_posix()}")
os.environ["AUTH_DISABLED"] = "1"   # 로컬 전용. 배포 환경에는 절대 설정하지 않는다.
os.environ["AUTO_COLLECT"] = "0"    # 로컬에서 Redash 로 나가지 않게

import socket  # noqa: E402

import uvicorn  # noqa: E402

PORT = int(os.getenv("DEV_PORT", "8000"))


def _port_taken(port: int) -> bool:
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    # 이미 떠 있는데 또 띄우면, 먼저 뜬 쪽이 포트를 쥔 채 옛 코드를 서빙해 수정이 반영 안 된 것처럼 보인다.
    if _port_taken(PORT):
        raise SystemExit(f"{PORT} 포트에 이미 서버가 떠 있습니다. 그쪽을 끄거나(Ctrl+C) "
                         f"DEV_PORT=8001 python scripts/dev.py 로 다른 포트를 쓰세요.")
    print(f"http://127.0.0.1:{PORT}  (Ctrl+C 로 종료)")
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT,
                reload=True, reload_dirs=[str(ROOT / "app")], log_level="warning")
