import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


@pytest.mark.django_db
def test_migrations_are_complete():
    # Nenhuma mudança de modelo sem migração.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db
def test_roles_have_expected_permissions(roles):
    operator = set(roles["operator"].permissions.values_list("codename", flat=True))
    admin = set(roles["admin"].permissions.values_list("codename", flat=True))
    assert {"post_event", "void_event", "change_status", "add_match", "change_stagecriterion"} <= operator
    assert "add_user" not in operator and "change_user" not in operator
    assert {"add_user", "change_user", "delete_user"} <= admin
    assert operator <= admin


@pytest.mark.django_db
def test_league_stage_gets_single_group():
    from tests.factories import make_league

    league = make_league(4)
    assert league["stage"].groups.count() == 1
