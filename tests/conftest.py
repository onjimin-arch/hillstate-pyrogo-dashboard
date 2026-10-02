import os

os.environ["AUTO_COLLECT"] = "0"     # 테스트가 .env 의 실제 Redash 로 나가지 않게
os.environ["AUTH_DISABLED"] = "1"    # 로그인 헤더 없는 기존 테스트용. 접근 제어 테스트는 이 값을 지운다.
