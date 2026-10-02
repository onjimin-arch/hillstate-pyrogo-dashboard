# 로봇 배송 KPI 대시보드

FastAPI + htmx 웹앱. 사내 Redash 에서 배송 데이터를 수집해 로봇 연계배송 KPI 를 보여준다.
지표·용어 정의는 별도 설계서, 아키텍처·배포 규칙은 [CLAUDE.md](CLAUDE.md) 를 본다.

## 로컬 개발 환경

```bash
git clone https://github.com/onjimin-arch/hillstate-pyrogo-dashboard.git
cd hillstate-pyrogo-dashboard

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env      # 값을 채운다 (아래 참고)
```

`.env` 는 git 에 올라가지 않는다. 로컬 개발이라면 최소 이렇게 두면 바로 뜬다.

```ini
DATABASE_URL=sqlite:///C:/전체/경로/hillstate-pyrogo-dashboard/data/local.db
AUTH_DISABLED=1      # 로컬 전용. 배포 환경에는 절대 넣지 않는다
AUTO_COLLECT=0       # 로컬에서 실수로 Redash 를 호출하지 않도록
```

- **DB**: 로컬은 SQLite 로 충분하다(경로는 절대경로 권장). 배포는 Postgres 필수.
- **AUTH_DISABLED=1**: 회사 로그인 헤더 없이 관리자로 접속한다. 배포 환경에 넣으면
  누구나 관리자가 되므로 금지.
- **Redash 수집을 로컬에서 쓰려면** `REDASH_URL`·`REDASH_API_KEY`·`REDASH_QUERY_ID` 가 필요하다.
  운영 키를 개인 PC 에 두는 것은 권장하지 않는다. 데이터가 필요하면 Redash 추출 xlsx 로 적재한다
  (아래 참고).

### 실행

```bash
run.bat
```

또는 VS Code 에서 **F5** (`대시보드 실행 (디버그)`) → http://localhost:8080

### 테스트

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

SQLite 인메모리를 쓰므로 DB 설정 없이 돌아간다. **푸시 전에 반드시 통과시킨다**
(CI 가 없어서 이게 유일한 안전망이다).

### 데이터 없이 화면만 보기

DB 가 비어 있으면 지표가 전부 `-` 로 나온다. Redash 추출 xlsx 가 있으면 적재할 수 있다.

```bash
.venv\Scripts\python.exe scripts\collect.py --xlsx "추출본.xlsx" --full
```

`배달ID` 가 PK 라서 여러 번 돌려도 중복이 쌓이지 않는다.
VS Code 에서는 `엑셀로 적재 (--xlsx)` 설정으로도 실행된다.

## 공동 작업 규칙

두 명이 같이 관리한다. `main` 에 브랜치 보호가 걸려 있지 않으므로 **규칙으로 지킨다.**

1. **`main` 에 직접 푸시하지 않는다.** 브랜치를 따고 PR 로 올린다.
   ```bash
   git checkout main && git pull
   git checkout -b fix/무엇을-고치는지
   ```
2. **푸시 전에 `pytest` 를 통과시킨다.** CI 가 없다.
3. **PR 은 서로 한 번 본다.** 특히 아래를 건드리면 꼭 리뷰를 받는다.
   - `app/kpi.py` 집계 로직 — 숫자가 조용히 틀어지면 알아채기 어렵다
   - `app/collector.py` 수집 — `raw_orders` 를 깨면 데이터가 날아간다
   - 환경변수·인증(`auth_gate`) 관련
4. **이 레포는 공개(public)** 다. 커밋 메시지·코드·문서에 다음을 넣지 않는다.
   - 가맹점명·라이더 정보·주소 등 개인정보
   - 미공개 경영수치(매출, 손실금 등)
   - 사내 시스템 주소·키

### 건드리면 안 되는 경계

- `raw_orders` 는 **수집 배치만** 쓴다. 웹앱이 쓰지 않는다.
- `robot_order_notes` 는 **웹앱 수기입력만** 쓴다. 수집이 덮어쓰지 않는다.
- 이 경계를 깨면 사용자가 화면에서 입력한 결과 구분·특이사항이 다음 수집 때 사라진다.

## 배포

사내 앱 배포 플랫폼에서 이 레포를 빌드한다. 자세한 규칙은 [CLAUDE.md](CLAUDE.md) 참고.

- 환경변수는 플랫폼 화면에서 넣는다. 코드·레포에 시크릿을 두지 않는다.
- 컨테이너는 배포마다 초기화된다. 로컬 파일에 데이터를 저장하지 않는다.
- `TZ` 를 설정하지 않으면 컨테이너가 UTC 로 돌아 날짜 기준(D-1, 주 경계)이 하루 밀린다.
  한국 운영이면 `TZ=Asia/Seoul` 을 넣는다.
- 환경변수만 바꿀 때는 **저장(앱 재시작)** 으로 충분하다. 재배포는 코드가 바뀐 경우에만.

## 구조

```
app/
  main.py        FastAPI 라우트, 인증 게이트
  kpi.py         기간 해석 + 지표 집계
  collector.py   Redash 수집 → raw_orders upsert
  scheduler.py   앱 내 주기 수집 스레드
  importer.py    SQLite 업로드 적재 (/admin/import)
  ai.py          AI 질문 기능
  db.py          테이블 정의, Redash 컬럼 매핑
  templates/     Jinja + htmx
  static/        CSS, 차트·htmx 번들
scripts/collect.py   배치 수집 CLI
tests/               pytest (SQLite 인메모리)
config.yaml          단지·필터·목표값 설정
```
