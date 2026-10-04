"""Id de requisição (`X-Request-ID`): o header do cliente só é aceito quando cabe em
`AuditLog.request_id` (1 a 64 caracteres seguros). Um header gigante não derruba as
escritas auditadas com 500 nem vai para a resposta e os logs: o id é gerado."""

from __future__ import annotations

import re

import pytest
from django.test import Client

from observability import audit
from observability.logging import request_id_var
from observability.middleware import request_id_from
from observability.models import AuditLog
from tests.factories import make_league, make_match

pytestmark = pytest.mark.django_db

PASSWORD = "senha-forte-123"
HEX32 = re.compile(r"[0-9a-f]{32}")


def login(client: Client, rid: str):
    client.get("/api/auth/me")
    return client.post(
        "/api/auth/login",
        {"username": "operador", "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": client.cookies["csrftoken"].value, "X-Request-ID": rid},
    )


@pytest.mark.parametrize(
    "value, kept",
    [
        ("abc-123", True),
        ("Req.1:2_3-4", True),
        ("x" * 64, True),
        ("x" * 65, False),
        ("", False),
        ("tem espaço", False),
        ("quebra\nde-linha", False),
        ("<script>", False),
        ("ação", False),
    ],
)
def test_request_id_from_accepts_only_safe_short_ids(value, kept):
    rid = request_id_from(value)
    if kept:
        assert rid == value
    else:
        assert HEX32.fullmatch(rid)


def test_oversized_request_id_does_not_break_login_audit(operator_user):
    client = Client(enforce_csrf_checks=True)
    header = "x" * 200
    response = login(client, header)
    assert response.status_code == 200, response.content.decode()
    entry = AuditLog.objects.get(action="auth.login")
    assert HEX32.fullmatch(entry.request_id) and entry.request_id != header
    assert response["X-Request-ID"] == entry.request_id  # o id gerado volta na resposta


def test_oversized_request_id_does_not_break_failed_login_audit(operator_user):
    client = Client(enforce_csrf_checks=True)
    client.get("/api/auth/me")
    response = client.post(
        "/api/auth/login",
        {"username": "operador", "password": "errada"},
        content_type="application/json",
        headers={"X-CSRFToken": client.cookies["csrftoken"].value, "X-Request-ID": "y" * 65},
    )
    assert response.status_code == 401
    entry = AuditLog.objects.get(action="auth.login_failed")
    assert HEX32.fullmatch(entry.request_id)


def test_valid_request_id_is_kept_audited_and_echoed(operator_user):
    client = Client(enforce_csrf_checks=True)
    response = login(client, "abc-123")
    assert response.status_code == 200
    assert response["X-Request-ID"] == "abc-123"
    assert AuditLog.objects.get(action="auth.login").request_id == "abc-123"


def test_ops_write_with_oversized_request_id_is_201(operator_client):
    league = make_league(n_teams=2)
    match = make_match(league["stage"], *league["teams"])
    response = operator_client.post(
        f"/api/ops/matches/{match.id}/events",
        {"type": "match_start"},
        content_type="application/json",
        headers={"Idempotency-Key": "rid-1", "X-Request-ID": "z" * 300},
    )
    assert response.status_code == 201, response.content.decode()
    entry = AuditLog.objects.get(action="event.create", match_id=match.id)
    assert HEX32.fullmatch(entry.request_id) and response["X-Request-ID"] == entry.request_id


def test_audit_record_truncates_request_id_as_second_guard(operator_user):
    token = request_id_var.set("r" * 100)  # ex.: contexto montado fora do middleware
    try:
        entry = audit.record("test.action", actor=operator_user)
    finally:
        request_id_var.reset(token)
    entry.refresh_from_db()
    assert entry.request_id == "r" * 64
