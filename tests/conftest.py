import pytest
from django.contrib.auth.models import Group
from django.core.cache import caches
from django.test import Client

from accounts.roles import ADMINISTRATOR, OPERATOR, sync_roles


@pytest.fixture(autouse=True)
def _read_cache_off(settings):
    """Micro-cache das leituras (api/read.py) desligado por padrão: muitos testes mexem
    no ORM sem publicar mensagem no outbox e leem de novo. Os testes do cache religam
    (`settings.READ_CACHE_SECONDS = 5`)."""
    settings.READ_CACHE_SECONDS = 0
    caches["reads"].clear()
    yield
    caches["reads"].clear()


@pytest.fixture
def roles(db):
    sync_roles()
    return {"operator": Group.objects.get(name=OPERATOR), "admin": Group.objects.get(name=ADMINISTRATOR)}


@pytest.fixture
def operator_user(django_user_model, roles):
    user = django_user_model.objects.create_user("operador", password="senha-forte-123", is_staff=True)
    user.groups.add(roles["operator"])
    return user


@pytest.fixture
def admin_user_fdr(django_user_model, roles):
    user = django_user_model.objects.create_user("administrador", password="senha-forte-123", is_staff=True)
    user.groups.add(roles["admin"])
    return user


@pytest.fixture
def plain_user(django_user_model, db):
    return django_user_model.objects.create_user("visitante", password="senha-forte-123")


@pytest.fixture
def operator_client(operator_user):
    client = Client()
    client.force_login(operator_user)
    return client


@pytest.fixture
def admin_client_fdr(admin_user_fdr):
    client = Client()
    client.force_login(admin_user_fdr)
    return client
