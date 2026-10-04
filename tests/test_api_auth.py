"""Autenticação e permissões (fase 1 do plano): o operador entra por
`/api/auth/login` (sessão com CSRF de verdade), é barrado na gestão de usuários
(exclusiva do administrador) e as rotas de operação respondem 401 sem login e
403 sem permissão. Auditoria de login, falha e logout."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from observability.models import AuditLog
from tests.factories import make_league, make_match

pytestmark = pytest.mark.django_db

PASSWORD = "senha-forte-123"


def csrf_cookie(client: Client) -> str:
    return client.cookies["csrftoken"].value


def post_json(client: Client, path: str, body: dict | None = None, **headers):
    return client.post(path, body or {}, content_type="application/json", headers=headers)


def prime_csrf(client: Client) -> dict:
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def match(db):
    league = make_league(n_teams=2)
    return make_match(league["stage"], league["teams"][0], league["teams"][1])


def ops_requests(match_id: int, event_id: int = 1) -> list[tuple[str, str, dict | None, dict]]:
    """As quatro rotas de operação, com corpo e headers válidos."""
    return [
        ("post", f"/api/ops/matches/{match_id}/events", {"type": "match_start"}, {"Idempotency-Key": "k-1"}),
        ("post", f"/api/ops/matches/{match_id}/events/{event_id}/void", {"reason": "erro"}, {}),
        ("post", f"/api/ops/matches/{match_id}/status", {"action": "postpone"}, {"Idempotency-Key": "k-2"}),
        ("get", "/api/ops/catalog", None, {}),
    ]


def call(client: Client, method: str, path: str, body, headers):
    if method == "get":
        return client.get(path, headers=headers)
    return client.post(path, body, content_type="application/json", headers=headers)


# --- Login ------------------------------------------------------------------------------


def test_operator_logs_in_with_csrf_and_session_rotates(operator_user):
    client = Client(enforce_csrf_checks=True)
    anonymous = client.get("/api/auth/me")
    assert anonymous.status_code == 200 and anonymous["Cache-Control"] == "no-store"
    me = anonymous.json()
    assert me["authenticated"] is False and me["user"] is None and me["csrf_token"]
    assert me["server_time"].endswith("Z")  # relógio do operador já no login
    assert "csrftoken" in anonymous.cookies  # o GET sempre seta o cookie CSRF
    old_session = client.session.session_key  # sessão anônima já aberta
    old_csrf = csrf_cookie(client)

    # Sem o header CSRF, o login é recusado mesmo com a senha certa.
    refused = post_json(client, "/api/auth/login", {"username": "operador", "password": PASSWORD})
    assert refused.status_code == 403 and refused.json()["code"] == "csrf_failed"

    response = post_json(client, "/api/auth/login", {"username": "operador", "password": PASSWORD}, **{"X-CSRFToken": old_csrf})
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["user"] == {
        "id": operator_user.id,
        "username": "operador",
        "name": "operador",
        "roles": ["Operador"],
        "permissions": {
            "post_event": True,
            "void_event": True,
            "change_status": True,
            "manage_users": False,
            "admin_site": True,
        },
    }
    assert body["csrf_token"]
    # O login troca a chave da sessão e o token CSRF.
    assert client.cookies["sessionid"].value != old_session
    assert csrf_cookie(client) != old_csrf

    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True and me["user"]["username"] == "operador"
    entry = AuditLog.objects.get(action="auth.login")
    assert entry.actor_id == operator_user.id and entry.actor_username == "operador"


def test_csrf_token_from_me_body_is_accepted(operator_user):
    client = Client(enforce_csrf_checks=True)
    token = prime_csrf(client)["csrf_token"]  # o front guarda este valor quando não lê o cookie
    response = post_json(client, "/api/auth/login", {"username": "operador", "password": PASSWORD}, **{"X-CSRFToken": token})
    assert response.status_code == 200


def test_wrong_password_unknown_and_inactive_user_are_401(operator_user, django_user_model):
    django_user_model.objects.create_user("inativo", password=PASSWORD, is_active=False)
    client = Client(enforce_csrf_checks=True)
    prime_csrf(client)
    headers = {"X-CSRFToken": csrf_cookie(client)}
    for username, password in [("operador", "senha-errada"), ("ninguem", PASSWORD), ("inativo", PASSWORD)]:
        response = post_json(client, "/api/auth/login", {"username": username, "password": password}, **headers)
        assert response.status_code == 401, username
        body = response.json()
        assert body["code"] == "invalid_credentials" and body["message"] and body["details"] == {}
    assert client.get("/api/auth/me").json()["authenticated"] is False
    failures = AuditLog.objects.filter(action="auth.login_failed").order_by("id")
    assert [entry.data["username"] for entry in failures] == ["operador", "ninguem", "inativo"]
    assert all(entry.actor_id is None for entry in failures)

    missing = post_json(client, "/api/auth/login", {"username": "operador"}, **headers)
    assert missing.status_code == 400
    assert missing.json()["code"] == "invalid_input" and missing.json()["details"]["field"] == "password"


def test_logout_requires_csrf_and_ends_session(operator_user, match):
    client = Client(enforce_csrf_checks=True)
    client.force_login(operator_user)
    prime_csrf(client)
    refused = post_json(client, "/api/auth/logout")
    assert refused.status_code == 403 and refused.json()["code"] == "csrf_failed"
    response = post_json(client, "/api/auth/logout", **{"X-CSRFToken": csrf_cookie(client)})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.get("/api/ops/catalog").status_code == 401
    assert AuditLog.objects.filter(action="auth.logout", actor=operator_user).count() == 1
    # sem sessão, logout continua inofensivo
    again = post_json(client, "/api/auth/logout", **{"X-CSRFToken": csrf_cookie(client)})
    assert again.status_code == 200 and AuditLog.objects.filter(action="auth.logout").count() == 1


def test_me_reports_administrator_permissions(admin_client_fdr):
    me = admin_client_fdr.get("/api/auth/me").json()
    assert me["authenticated"] is True and me["user"]["roles"] == ["Administrador"]
    assert all(me["user"]["permissions"].values())


# --- Gestão de usuários: só o administrador ----------------------------------------------------


def test_operator_is_barred_from_user_management(operator_client, admin_client_fdr):
    assert operator_client.get("/admin/").status_code == 200  # entra no admin (cadastros)
    assert operator_client.get("/admin/accounts/user/").status_code == 403
    assert operator_client.get("/admin/accounts/user/add/").status_code == 403
    assert operator_client.get("/admin/auth/group/").status_code == 403
    assert admin_client_fdr.get("/admin/accounts/user/").status_code == 200
    assert admin_client_fdr.get("/admin/accounts/user/add/").status_code == 200


# --- 401 e 403 em todas as rotas de operação ---------------------------------------------------


@pytest.mark.parametrize("enforce_csrf", [False, True])
def test_every_ops_route_is_401_without_login(match, enforce_csrf):
    client = Client(enforce_csrf_checks=enforce_csrf)  # sem token: 401 vem antes do CSRF
    for method, path, body, headers in ops_requests(match.id) + ops_requests(999999):
        response = call(client, method, path, body, headers)
        assert response.status_code == 401, path
        payload = response.json()
        assert payload["code"] == "not_authenticated" and payload["message"] and payload["details"] == {}


def test_every_ops_route_is_403_without_permission(match, plain_user):
    client = Client()
    client.force_login(plain_user)
    for method, path, body, headers in ops_requests(match.id):
        response = call(client, method, path, body, headers)
        assert response.status_code == 403, path
        assert response.json()["code"] == "permission_denied"
    assert match.events.count() == 0


def test_permissions_are_per_action(match, django_user_model):
    user = django_user_model.objects.create_user("so-lanca", password=PASSWORD)
    user.user_permissions.add(Permission.objects.get(codename="post_event", content_type__app_label="matches"))
    client = Client()
    client.force_login(user)
    events, void, status, catalog = ops_requests(match.id)
    created = call(client, *events)
    assert created.status_code == 201
    event_id = created.json()["event"]["id"]
    assert call(client, *ops_requests(match.id, event_id)[1]).status_code == 403
    assert call(client, *status).status_code == 403
    assert call(client, *catalog).status_code == 200
    me = client.get("/api/auth/me").json()
    assert me["user"]["permissions"] == {
        "post_event": True,
        "void_event": False,
        "change_status": False,
        "manage_users": False,
        "admin_site": False,
    }


def test_ops_post_checks_csrf_for_logged_in_operator(match, operator_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(operator_user)
    prime_csrf(client)
    events, _void, _status, catalog = ops_requests(match.id)
    refused = call(client, *events)
    assert refused.status_code == 403 and refused.json()["code"] == "csrf_failed"
    assert match.events.count() == 0
    assert call(client, *catalog).status_code == 200  # GET não exige token
    method, path, body, headers = events
    accepted = call(client, method, path, body, {**headers, "X-CSRFToken": csrf_cookie(client)})
    assert accepted.status_code == 201


# --- Auditoria das sessões: API e Django Admin ----------------------------------------------


def session_rows():
    return list(AuditLog.objects.filter(action__startswith="auth.").order_by("id"))


def test_api_login_and_logout_write_exactly_one_row_each(operator_user):
    """A auditoria vem dos sinais de autenticação: nada em dobro na API."""
    client = Client(enforce_csrf_checks=True)
    prime_csrf(client)
    response = post_json(client, "/api/auth/login", {"username": "operador", "password": PASSWORD}, **{"X-CSRFToken": csrf_cookie(client)})
    assert response.status_code == 200
    response = post_json(client, "/api/auth/logout", **{"X-CSRFToken": csrf_cookie(client)})
    assert response.status_code == 200
    rows = session_rows()
    assert [row.action for row in rows] == ["auth.login", "auth.logout"]
    assert all(row.actor_id == operator_user.id and row.ip == "127.0.0.1" for row in rows)
    assert rows[0].object_type == "accounts.user" and rows[0].object_id == str(operator_user.pk)


def test_admin_login_failure_success_and_logout_are_audited(operator_user):
    client = Client()
    extra = {"REMOTE_ADDR": "10.1.2.3"}
    failed = client.post("/admin/login/?next=/admin/", {"username": "operador", "password": "senha-errada"}, **extra)
    assert failed.status_code == 200  # formulário de novo, com o erro
    (row,) = session_rows()
    assert row.action == "auth.login_failed" and row.actor_id is None and row.ip == "10.1.2.3"
    assert row.data == {"username": "operador"}  # a senha nunca vai para a auditoria

    response = client.post("/admin/login/?next=/admin/", {"username": "operador", "password": PASSWORD}, **extra)
    assert response.status_code == 302
    assert client.get("/admin/").status_code == 200
    login = session_rows()[-1]
    assert login.action == "auth.login" and login.actor_id == operator_user.id
    assert login.actor_username == "operador" and login.ip == "10.1.2.3"

    response = client.post("/admin/logout/", **extra)
    assert response.status_code == 200
    logout = session_rows()[-1]
    assert logout.action == "auth.logout" and logout.actor_id == operator_user.id and logout.ip == "10.1.2.3"
    assert [row.action for row in session_rows()] == ["auth.login_failed", "auth.login", "auth.logout"]


def test_admin_login_failure_never_stores_the_password(operator_user):
    Client().post("/admin/login/", {"username": "ninguem", "password": "segredo-que-nao-pode-vazar"})
    row = AuditLog.objects.get(action="auth.login_failed")
    assert row.data == {"username": "ninguem"}
    assert "segredo" not in str(row.data)


def test_admin_password_change_is_audited(operator_user):
    client = Client()
    client.force_login(operator_user)
    new_password = "Outra-senha-forte-456"
    invalid = client.post(
        "/admin/password_change/", {"old_password": "errada", "new_password1": new_password, "new_password2": new_password}
    )
    assert invalid.status_code == 200
    assert not AuditLog.objects.filter(action="auth.password_change").exists()
    response = client.post(
        "/admin/password_change/", {"old_password": PASSWORD, "new_password1": new_password, "new_password2": new_password}
    )
    assert response.status_code == 302
    row = AuditLog.objects.get(action="auth.password_change")
    assert row.actor_id == operator_user.id and row.object_id == str(operator_user.pk)
    assert new_password not in str(row.data) and PASSWORD not in str(row.data)
    operator_user.refresh_from_db()
    assert operator_user.check_password(new_password)
