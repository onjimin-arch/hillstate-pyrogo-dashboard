# 힐스테이트 푸르지오 수원 배송 KPI 대시보드

설계서: `../힐스테이트_푸르지오_수원_KPI_대시보드_설계서.md` (용어·지표 정의의 원본)

## 구조
- `app/collector.py` Redash API 수집 → `raw_orders` upsert (최근 7일 롤링). 수집 배치만 raw_orders에 쓴다.
- `app/kpi.py` 기간 해석(D-1, 주=월~일) + 지표 집계. 모수=`config.yaml`의 단지+상점구분(`store_types`: 로드샵·B2B)+DROP_FINISHED.
- `app/main.py` FastAPI. `/` 대시보드, `/robots` 로봇 건 리스트(결과 구분·특이사항·집계 제외 입력), `/stores` 상점 현황(명부 기준 동의 현황 + 액티브 상점 + 상점별 표. 기간 선택 없이 **최근 30일 고정**이라 상단 기간 바를 쓰지 않는다).
- `robot_order_notes`는 웹앱만 쓴다. 수집이 덮어쓰지 않는다.
- `store_consent` 상점별 동의 현황(`REDASH_CONSENT_QUERY_ID`, 주문과 다른 쿼리). 모수가 **수원시 승인 상점 전체 + 이 건물에 주문이 있던 상점**이라 주문 없는 상점도 들어온다. 주문 기준 동의율(`kpi.consent_stats`)과 명부 기준(`kpi.area_consent_stats`)은 **모수가 달라 섞지 않는다**. 주문 행의 동의값이 비었으면 명부로 메운다. `주문건수`는 조회 기간에 따라 달라지므로 저장하지 않는다.
- `app/neubility.py` 뉴빌리티 수기 시트(CSV/XLSX) → `robot_trips`. 관리자 업로드(`/admin/trips/upload`)로만 들어오고 수집 배치와 무관하다. **호수(세대)는 저장하지 않는다**(개별 배송지). 동은 보관하되 표본이 동당 2건 수준이라 화면에는 쓰지 않는다. 시트는 분 단위라 Redash 값과 수십 초 어긋나므로 운영 참고값이지 KPI 원본이 아니다.
- 집계 제외: 로봇 품질(적시·성공률)·로봇 평균 시간에서만 제외, 건수·이용률은 유지.

## 실행 (LUCA Apps 배포 규칙)
- 환경변수: `DATABASE_URL`(Postgres, 필수), `PORT`(기본 8080), Redash/OpenAI 키는 `.env.example` 참고. 시크릿은 코드에 넣지 않는다.
- 서버: `Procfile` (`uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}`). `/` 가 200 이면 헬스체크 통과. 테이블은 시작 시 `create_all` 로 재실행 안전하게 생성.
- 로그인: 앱 앞단 회사 로그인 사용. 접속자는 `x-amzn-oidc-data` JWT 의 email → `updated_by`. 웹훅 엔드포인트 없음(추가 시 `/webhooks/` + 서명 검증 + 401).
- 로컬 파일에 데이터를 저장하지 않는다(컨테이너는 배포마다 초기화). 수집: `python scripts/collect.py` (같은 `DATABASE_URL` 로 실행, `--full` 백필, `--xlsx` 엑셀 적재)
- 과거분 동의상태 채우기: `/admin/import` 의 「동의상태만 채우기」 또는 `python scripts/backfill_consent.py`. 이미 쌓인 주문 전 기간(= raw_orders 접수일 범위)을 월 단위로 다시 받아 `상점로봇동의상태`·`상점ID` **만** 덮어쓴다. 전체 재수집과 달리 시간·상태 컬럼은 건드리지 않고(쿼리 ver 변경에 기존 KPI 가 휩쓸리지 않게), DB 에 없는 배달ID 는 만들지 않으며, `collect_log` 에도 남기지 않는다(남기면 '마지막 수집 시각'이 잘못 갱신된다).
- 접근 제어: 전 라우트(`/health`·정적 파일 제외)에 `auth_gate`. x-amzn-oidc-data 이메일이 `app_users` 에 등록(또는 기본 관리자 `jmlee@barogo.com`, 환경변수 `ADMIN_EMAILS` 로 추가)된 경우만 통과. 권한 admin/user, 관리는 `/settings`(관리자 전용). 로그인 헤더가 없으면 401(단, `/` 는 플랫폼 헬스체크용으로 200+안내문만). 로컬 개발은 `AUTH_DISABLED=1`(헤더 없는 요청만 관리자 취급, 배포 환경에는 설정 금지).
- 레이아웃·디자인: `../UI_UX_디자인_가이드.md` 기준(라이트 단일 테마, 오렌지 브랜드 토큰, 사이드바 240px + 상단바 56px). 색은 `app/static/app.css` 의 `:root` 토큰만 쓰고 차트는 `--s1~s7`. 설정 화면 `/settings`.
- 데이터 가져오기: `/admin/import` (관리자만). 로컬 SQLite 업로드 → 임시 디스크 파일 → 백그라운드 스레드가 1,000행씩 `ON CONFLICT DO NOTHING` 적재 후 id 시퀀스 보정(`app/importer.py`). 진행 상태는 프로세스 메모리(컨테이너 1개 전제), 임시 파일은 끝나면 삭제. `IMPORT_MAX_MB`(기본 500).
- 자동 수집: 앱 시작 시 `app/scheduler.py` 스레드가 `COLLECT_INTERVAL_MIN`(기본 60)분마다 수집. 이력이 없으면 첫 실행은 전체 백필. `AUTO_COLLECT=0` 으로 끔(외부 스케줄러 사용 시). REDASH_* 미설정이면 시작 로그에 경고만 남기고 안 돈다. `/admin/import` 는 실행 중인 쿼리 ID 와, 마지막 응답에 우리가 읽는 컬럼이 다 있었는지를 같이 보여준다(값이 전부 '-' 로 나올 때 여기부터 본다).
- 로컬 화면 확인(배포 없이): `python scripts/seed_dev.py` 로 `data/dev.db` 에 가상 데이터를 깔고 `python scripts/dev.py` → http://127.0.0.1:8000. `app/*.py` 는 자동 재시작, 템플릿은 새로고침, CSS 는 Ctrl+F5. 시드는 전부 가상 값이며 `data/` 는 gitignore 대상이다.
- 테스트: `python -m pytest tests -q` (SQLite 인메모리 사용)

## 미확정(설계서 §5) — 값이 바뀔 수 있는 곳
- 성공률 정의(잠정: 수기 결과 구분), 취소 건 표현, baseline 재산출 대조, Redash 쿼리 ID
- 적시 배송률 7분 기준의 범위: 뉴빌리티 기록상 ④ 8분 중 주행 5분·고객 수령 대기 2분이라, 기준을 '로봇 주행만'으로 볼지 '고객 수령까지'로 볼지에 따라 달성률이 크게 갈린다(현 기준 45%)
- 구간 강조 임계값(`kpi.SEG_FLAG_*`: 중앙값 2배·5분·표본 5건)은 실데이터 분포를 보고 조정할 것
