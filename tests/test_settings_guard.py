"""Configuração fora do desenvolvimento: chave secreta obrigatória e pool de conexões.

Cada caso carrega `config.settings` num processo novo, com um ambiente limpo (sem
DJANGO_* nem DB_POOL* herdados), como o contêiner faria. Os demais testes rodam com
DEBUG=1 ou o padrão e não passam pela trava.
"""

import json
import re
import secrets
import subprocess
import sys

import pytest

from tests.deploy_files import BASE_DIR, clean_env, dockerfile_env, dockerfile_instructions, dockerfile_run, env_example_value

DEV_SECRET_KEY = "dev-insecure-futebol-de-raizes-troque-em-producao"


def run_python(code: str, **overrides: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BASE_DIR, env=clean_env(**overrides), capture_output=True, text=True, timeout=60
    )


DUMP_SETTINGS = (
    "import json, config.settings as s; "
    "print(json.dumps({'debug': s.DEBUG, 'key': s.SECRET_KEY, 'max_age': s.WHITENOISE_MAX_AGE, "
    "'static': s.STORAGES['staticfiles']['BACKEND'], 'db': s.DATABASES['default']}, default=str))"
)


def load_settings(**overrides: str | None) -> dict:
    result = run_python(DUMP_SETTINGS, **overrides)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def strong_key() -> str:
    return secrets.token_urlsafe(50)


# --- Chave secreta ---------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(None, id="sem-chave"),
        pytest.param("", id="vazia"),
        pytest.param(DEV_SECRET_KEY, id="padrao-do-codigo"),
        pytest.param(env_example_value("DJANGO_SECRET_KEY"), id="exemplo-do-env-example"),
        pytest.param("curta-" + secrets.token_hex(10), id="menos-de-50"),
        pytest.param("ab" * 40, id="poucos-caracteres-distintos"),
        pytest.param("django-insecure-" + secrets.token_urlsafe(50), id="prefixo-inseguro"),
    ],
)
def test_without_debug_a_missing_weak_or_public_key_stops_the_process(key):
    result = run_python("import config.settings", DJANGO_DEBUG="0", DJANGO_SECRET_KEY=key)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "Defina DJANGO_SECRET_KEY" in result.stderr


def test_django_setup_fails_fast_without_key_and_debug():
    """`docker run` da imagem (DEBUG=0) sem chave: falha na partida, não serve nada."""
    result = run_python("import django; django.setup()", DJANGO_SETTINGS_MODULE="config.settings", DJANGO_DEBUG="0")
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr


def test_without_debug_a_strong_key_loads_production_settings():
    key = strong_key()
    loaded = load_settings(DJANGO_DEBUG="0", DJANGO_SECRET_KEY=key)
    assert loaded["debug"] is False and loaded["key"] == key
    # Produção: estáticos com hash e comprimidos; prazo curto só para URL sem hash.
    assert loaded["static"] == "core.storage.ModuleManifestStaticFilesStorage"
    assert loaded["max_age"] == 3600


@pytest.mark.parametrize("key", [None, "", env_example_value("DJANGO_SECRET_KEY")])
def test_debug_keeps_working_with_the_development_keys(key):
    loaded = load_settings(DJANGO_DEBUG="1", DJANGO_SECRET_KEY=key)
    assert loaded["debug"] is True
    assert loaded["key"] == (key or DEV_SECRET_KEY)
    assert loaded["max_age"] == 60


def check_deploy(**overrides: str | None) -> str:
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy"],
        cwd=BASE_DIR,
        env=clean_env(DB_NAME="sem-banco", **overrides),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout + result.stderr


def test_guard_agrees_with_check_deploy_secret_key_rule():
    """Chave aceita pela trava também passa no `check --deploy` (sem security.W009)."""
    output = check_deploy(DJANGO_DEBUG="0", DJANGO_SECRET_KEY=strong_key())
    assert "ImproperlyConfigured" not in output and "System check" in output
    assert "security.W009" not in output
    # A chave de desenvolvimento (aceita só com DEBUG=1) é a que o check acusa.
    assert "security.W009" in check_deploy(DJANGO_DEBUG="1")


# --- Pool de conexões --------------------------------------------------------------


def test_pool_is_off_by_default():
    db = load_settings(DJANGO_DEBUG="1")["db"]
    assert "pool" not in db["OPTIONS"] and db["CONN_MAX_AGE"] == 0


def test_db_pool_turns_on_psycopg_pool_with_conn_max_age_zero():
    db = load_settings(DJANGO_DEBUG="0", DJANGO_SECRET_KEY=strong_key(), DB_POOL="1")["db"]
    assert db["OPTIONS"]["pool"] == {"min_size": 2, "max_size": 20, "timeout": 10}
    assert db["CONN_MAX_AGE"] == 0  # o Django recusa pool com conexão persistente
    db = load_settings(DJANGO_DEBUG="1", DB_POOL="1", DB_POOL_MIN="1", DB_POOL_MAX="5")["db"]
    assert db["OPTIONS"]["pool"] == {"min_size": 1, "max_size": 5, "timeout": 10}


# --- Build da imagem ----------------------------------------------------------------


def test_image_runs_without_debug_and_has_no_baked_secret_key():
    image_env = dockerfile_env()
    assert image_env["DJANGO_DEBUG"] == "0"
    assert "DJANGO_SECRET_KEY" not in image_env  # nenhuma chave fica na imagem
    assert not any("DJANGO_SECRET_KEY" in rest for word, rest in dockerfile_instructions() if word in {"ENV", "ARG"})
    collect = dockerfile_run("collectstatic")
    assert "secrets.token_urlsafe" in collect  # chave aleatória, só deste RUN
    assert not re.search(r"DJANGO_SECRET_KEY=[\w-]", collect)  # nenhuma chave fixa no repositório


def test_image_build_collectstatic_step_passes_the_guard():
    """O RUN do collectstatic, como está no Dockerfile, roda sem chave no ambiente."""
    collect = dockerfile_run("collectstatic")
    result = subprocess.run(
        ["sh", "-c", collect + " --dry-run"],  # sem gravar nada no staticfiles/ do repositório
        cwd=BASE_DIR,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "Pretending to copy" in result.stdout
