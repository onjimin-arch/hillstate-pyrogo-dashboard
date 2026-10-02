# 힐스테이트 푸르지오 수원 배송 KPI 대시보드

설계서: `../힐스테이트_푸르지오_수원_KPI_대시보드_설계서.md` (용어·지표 정의의 원본)

## 구조
- `app/collector.py` Redash API 수집 → `raw_orders` upsert (최근 7일 롤링). 수집 배치만 raw_orders에 쓴다.
- `app/kpi.py` 기간 해석(D-1, 주=월~일) + 지표 집계. 모수=`config.yaml`의 단지+상점구분(로드샵)+DROP_FINISHED.
- `app/main.py` FastAPI. `/` 대시보드, `/robots` 로봇 건 리스트(결과 구분·특이사항·집계 제외 입력).
- `robot_order_notes`는 웹앱만 쓴다. 수집이 덮어쓰지 않는다.
- 집계 제외: 로봇 품질(적시·성공률)·로봇 평균 시간에서만 제외, 건수·이용률은 유지.

## 실행 (LUCA Apps 배포 규칙)
- 환경변수: `DATABASE_URL`(Postgres, 필수), `PORT`(기본 8080), Redash/OpenAI 키는 `.env.example` 참고. 시크릿은 코드에 넣지 않는다.
- 서버: `Procfile` (`uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}`). `/` 가 200 이면 헬스체크 통과. 테이블은 시작 시 `create_all` 로 재실행 안전하게 생성.
- 로그인: 앱 앞단 회사 로그인 사용. 접속자는 `x-amzn-oidc-data` JWT 의 email → `updated_by`. 웹훅 엔드포인트 없음(추가 시 `/webhooks/` + 서명 검증 + 401).
- 로컬 파일에 데이터를 저장하지 않는다(컨테이너는 배포마다 초기화). 수집: `python scripts/collect.py` (같은 `DATABASE_URL` 로 실행, `--full` 백필, `--xlsx` 엑셀 적재)
- 테스트: `python -m pytest tests -q` (SQLite 인메모리 사용)

## 미확정(설계서 §5) — 값이 바뀔 수 있는 곳
- 성공률 정의(잠정: 수기 결과 구분), 취소 건 표현, baseline 재산출 대조, Redash 쿼리 ID
