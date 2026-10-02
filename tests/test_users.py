import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_import import client, jwt  # noqa: E402,F401  (fixture 재사용)

ADMIN = jwt("jmlee@barogo.com")
W = {"x-requested-with": "x"}


def add(client, email, role="user", by=ADMIN):
    return client.post("/settings/users", json={"email": email, "role": role}, headers={**by, **W})


def test_gate_blocks_unregistered_and_anonymous(client):
    assert client.get("/", headers=jwt("who@barogo.com")).status_code == 403
    assert client.get("/robots", headers=jwt("who@barogo.com")).status_code == 403
    assert client.get("/health").status_code == 200
    r = client.get("/")                      # 헤더 없음: 플랫폼 헬스체크용 200, 데이터는 없음
    assert r.status_code == 200 and "KPI" not in r.text and "대시보드" not in r.text.replace("로봇 연계 배송 KPI 대시보드", "")
    assert client.get("/robots").status_code == 401
    html = client.get("/", headers={**jwt("who@barogo.com"), "accept": "text/html"})
    assert html.status_code == 403 and "등록된 사용자만" in html.text and "who@barogo.com" in html.text


def test_register_role_and_delete(client):
    assert add(client, "A@Barogo.com", "user").status_code == 200       # 대소문자 무시
    a = jwt("a@barogo.com")
    assert client.get("/", headers=a).status_code == 200
    assert client.get("/settings", headers=a).status_code == 200
    assert "사용자 등록" not in client.get("/settings", headers=a).text
    assert add(client, "b@barogo.com", by=a).status_code == 403            # 일반 사용자는 등록 불가
    assert add(client, "a@barogo.com", "admin").status_code == 200         # 관리자로 승격
    assert add(client, "b@barogo.com", by=a).status_code == 200
    assert "b@barogo.com" in client.get("/settings", headers=ADMIN).text
    d = client.post("/settings/users/delete", json={"email": "b@barogo.com"}, headers={**ADMIN, **W})
    assert d.status_code == 200
    assert client.get("/", headers=jwt("b@barogo.com")).status_code == 403   # 삭제되면 즉시 차단


def test_user_admin_guards(client):
    assert add(client, "not-an-email").status_code == 400
    assert add(client, "x@barogo.com", "root").status_code == 400
    assert add(client, "jmlee@barogo.com", "user").status_code == 400        # 기본 관리자 보호
    assert add(client, "other@barogo.com").status_code == 200
    d = client.post("/settings/users/delete", json={"email": "jmlee@barogo.com"}, headers={**ADMIN, **W})
    assert d.status_code == 400
    assert client.post("/settings/users", json={"email": "x@barogo.com", "role": "user"},
                       headers=ADMIN).status_code == 400                       # 커스텀 헤더 없음
    other_admin = jwt("other@barogo.com")
    add(client, "other@barogo.com", "admin")
    assert add(client, "other@barogo.com", "user", by=other_admin).status_code == 400   # 본인 변경 불가


def test_sidebar_menu(client):
    html = client.get("/", headers=ADMIN).text
    assert 'class="side"' in html and 'href="/settings"' in html and 'href="/admin/import"' in html
