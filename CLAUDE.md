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
- 접근 제어: 전 라우트(`/health`·정적 파일 제외)에 `auth_gate`. x-amzn-oidc-data 이메일이 `app_users` 에 등록(또는 기본 관리자 `jmlee@barogo.com`, 환경변수 `ADMIN_EMAILS` 로 추가)된 경우만 통과. 권한 admin/user, 관리는 `/settings`(관리자 전용). 로그인 헤더가 없으면 401(단, `/` 는 플랫폼 헬스체크용으로 200+안내문만). 로컬 개발은 `AUTH_DISABLED=1`(헤더 없는 요청만 관리자 취급, 배포 환경에는 설정 금지).
- 레이아웃: 좌측 사이드바 메뉴(`base.html`). 설정 화면 `/settings`.
- 데이터 가져오기: `/admin/import` (관리자만). 로컬 SQLite 업로드 → 임시 디스크 파일 → 백그라운드 스레드가 1,000행씩 `ON CONFLICT DO NOTHING` 적재 후 id 시퀀스 보정(`app/importer.py`). 진행 상태는 프로세스 메모리(컨테이너 1개 전제), 임시 파일은 끝나면 삭제. `IMPORT_MAX_MB`(기본 500).
- 자동 수집: 앱 시작 시 `app/scheduler.py` 스레드가 `COLLECT_INTERVAL_MIN`(기본 60)분마다 수집. 이력이 없으면 첫 실행은 전체 백필. `AUTO_COLLECT=0` 으로 끔(외부 스케줄러 사용 시). REDASH_* 미설정이면 시작 로그에 경고만 남기고 안 돈다.
- 테스트: `python -m pytest tests -q` (SQLite 인메모리 사용)

## 미확정(설계서 §5) — 값이 바뀔 수 있는 곳
- 성공률 정의(잠정: 수기 결과 구분), 취소 건 표현, baseline 재산출 대조, Redash 쿼리 ID
