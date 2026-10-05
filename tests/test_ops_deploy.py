"""Operação (fases 6 e 11): logs estruturados, rótulos de métricas, estáticos comprimidos,
compose e a imagem Docker.

A imagem é reproduzida sem Docker: os diretórios dos `COPY` do Dockerfile vão para uma
pasta temporária, o `RUN` do collectstatic roda como está escrito e o servidor sobe com
os `ENV` e o `CMD` da imagem (DEBUG=0, chave forte, pool ligado, como no compose).
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import brotli
import httpx
import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.signals import request_finished
from django.db import close_old_connections, connection
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from whitenoise.middleware import WhiteNoiseMiddleware

from config.settings import is_weak_secret_key
from core import timeutils
from observability.logging import JsonFormatter
from observability.metrics import metrics
from tests.deploy_files import BASE_DIR, clean_env, dockerfile_cmd, dockerfile_env, dockerfile_instructions, env_example_value
from tests.factories import make_league, make_match

STEP_TIMEOUT = 10.0
FOREVER = "max-age=315360000, public, immutable"  # WhiteNoise: arquivo com hash no nome


# --- Logs: uma linha JSON por registro --------------------------------------------


def test_json_formatter_drops_uvicorn_color_message():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started server process [%d]", (7,), None)
    record.color_message = "Started server process [\x1b[36m%d\x1b[0m]"
    record.path = "/"
    data = json.loads(JsonFormatter().format(record))
    assert data["msg"] == "Started server process [7]" and data["path"] == "/"
    assert "color_message" not in data


LOGGING_PROBE = r"""
import logging, logging.config, warnings
from uvicorn.config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)  # o uvicorn configura o logging antes de importar o app
import django
django.setup()
access = logging.getLogger("uvicorn.access")
print("ACCESS_HAS_HANDLERS", access.hasHandlers(), flush=True)
logging.getLogger("uvicorn.error").info(
    "Started server process [%d]", 42, extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"}
)
access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/health", "1.1", 200)
logging.getLogger("django.request").warning("Not Found: %s", "/nao-existe")
warnings.warn("aviso de teste")
logging.getLogger("fdr.http").info("request", extra={"path": "/", "route": "/"})
"""


@pytest.mark.parametrize("debug", ["0", "1"])
def test_logging_after_uvicorn_setup_is_json_only_and_without_access_duplicate(debug):
    """Ordem real: o uvicorn configura o logging, depois o Django aplica LOGGING por cima."""
    result = subprocess.run(
        [sys.executable, "-c", LOGGING_PROBE],
        cwd=BASE_DIR,
        env=clean_env(
            DJANGO_SETTINGS_MODULE="config.settings",
            DJANGO_DEBUG=debug,
            DJANGO_SECRET_KEY=secrets.token_urlsafe(50),
            LOG_FORMAT="json",
            LOG_LEVEL="INFO",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out_lines = result.stdout.splitlines()
    assert out_lines[0] == "ACCESS_HAS_HANDLERS False"  # o uvicorn não escreve log de acesso
    lines = [line for line in out_lines[1:] + result.stderr.splitlines() if line.strip()]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            pytest.fail(f"linha fora do JSON: {line!r}\n{result.stderr}")
    # Cada registro sai uma vez só (sem a cópia em texto do DEFAULT_LOGGING com DEBUG=1).
    assert [r["logger"] for r in records] == ["uvicorn.error", "django.request", "py.warnings", "fdr.http"]
    started = records[0]
    assert started["msg"] == "Started server process [42]" and "color_message" not in started


@pytest.mark.django_db
def test_http_metrics_label_home_static_and_unmatched_routes(client, caplog):
    def count(route: str, status: int) -> float:
        return metrics.value("fdr_http_requests_total", method="GET", route=route, status=status)

    expected = [("/", 200), ("/static/*", 200), ("unmatched", 404), ("/api/competitions", 200)]
    before = {key: count(*key) for key in expected}
    with caplog.at_level(logging.INFO, logger="fdr.http"):
        assert client.get("/").status_code == 200
        static = client.get("/static/css/app.css")
        assert static.status_code == 200
        assert static.getvalue()  # consumir fecha a resposta pelo caminho do cliente de teste
        assert client.get("/nao-existe-mesmo").status_code == 404
        assert client.get("/api/competitions").status_code == 200
    for key in expected:
        assert count(*key) == before[key] + 1, key
    routes = {record.path: record.route for record in caplog.records if record.name == "fdr.http"}
    assert routes["/"] == "/" and routes["/nao-existe-mesmo"] == "unmatched"
    assert "/static/css/app.css" not in routes  # estático não entra no log de acesso


# --- Estáticos: hash no nome, .gz e .br ----------------------------------------------


@contextlib.contextmanager
def production_static(root: Path):
    """Estáticos como em produção (DEBUG=0), gravados em `root`."""
    storages = {**settings.STORAGES, "staticfiles": {"BACKEND": "core.storage.ModuleManifestStaticFilesStorage"}}
    with override_settings(
        STATIC_ROOT=root,
        STORAGES=storages,
        WHITENOISE_USE_FINDERS=False,
        WHITENOISE_AUTOREFRESH=False,
        WHITENOISE_MAX_AGE=3600,
    ):
        yield


@pytest.fixture(scope="module")
def collected_static(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("staticfiles")
    with production_static(root):
        call_command("collectstatic", "--noinput", verbosity=0)
    return root


def manifest_paths(static_root: Path) -> dict[str, str]:
    return json.loads((static_root / "staticfiles.json").read_text())["paths"]


def test_collectstatic_writes_gzip_and_brotli_next_to_hashed_files(collected_static):
    paths = manifest_paths(collected_static)
    css = paths["css/app.css"]
    assert re.fullmatch(r"css/app\.[0-9a-f]{12}\.css", css)
    original = (collected_static / css).read_bytes()
    assert gzip.decompress((collected_static / f"{css}.gz").read_bytes()) == original
    compressed = (collected_static / f"{css}.br").read_bytes()
    assert brotli.decompress(compressed) == original and len(compressed) < len(original) / 3
    home = paths["js/home.js"]
    assert (collected_static / f"{home}.br").is_file() and (collected_static / f"{home}.gz").is_file()
    # Formato já comprimido não ganha cópia.
    font = next(name for name in paths.values() if name.endswith(".woff2"))
    assert not (collected_static / f"{font}.gz").exists() and not (collected_static / f"{font}.br").exists()


def test_hashed_modules_import_hashed_siblings(collected_static):
    paths = manifest_paths(collected_static)
    hashed = set(paths.values())
    source = (collected_static / paths["js/home.js"]).read_text()
    targets = re.findall(r"""\bfrom\s+["'](\.[^"']+)["']""", source)
    assert len(targets) >= 3, source[:500]
    for target in targets:
        assert re.fullmatch(r"\./[\w-]+\.[0-9a-f]{12}\.js", target), target
        assert f"js/{target[2:]}" in hashed, target


def test_whitenoise_serves_precompressed_files_and_caches_by_hash(collected_static):
    css = manifest_paths(collected_static)["css/app.css"]
    with production_static(collected_static):
        middleware = WhiteNoiseMiddleware(lambda request: HttpResponse(status=404))
        factory = RequestFactory()

        def get(path: str, encoding: str = "") -> HttpResponse:
            response = middleware(factory.get(path, HTTP_ACCEPT_ENCODING=encoding))
            # Fecha o arquivo sem disparar o request_finished (que fecharia o banco).
            request_finished.disconnect(close_old_connections)
            try:
                response.close()
            finally:
                request_finished.connect(close_old_connections)
            return response

        br = get(f"/static/{css}", "gzip, deflate, br")
        assert br.status_code == 200 and br["Content-Encoding"] == "br"
        assert br["Cache-Control"] == FOREVER and "Accept-Encoding" in br["Vary"]
        assert int(br["Content-Length"]) == (collected_static / f"{css}.br").stat().st_size
        assert get(f"/static/{css}", "gzip")["Content-Encoding"] == "gzip"
        identity = get(f"/static/{css}")
        assert not identity.has_header("Content-Encoding")
        assert int(identity["Content-Length"]) == (collected_static / css).stat().st_size
        # Sem hash no nome: prazo curto (WHITENOISE_MAX_AGE), nunca um ano.
        assert get("/static/css/app.css", "br")["Cache-Control"] == "max-age=3600, public"


# --- docker compose -------------------------------------------------------------------


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose ausente")


@pytest.fixture
def compose_dir(tmp_path) -> Path:
    """Cópia dos arquivos do compose, longe de um .env local do repositório."""
    shutil.copy(BASE_DIR / "docker-compose.yml", tmp_path)
    shutil.copytree(BASE_DIR / "deploy", tmp_path / "deploy")
    return tmp_path


def compose_config(directory: Path, *files: str, **env: str) -> subprocess.CompletedProcess:
    args = ["docker", "compose"]
    for name in files or ("docker-compose.yml",):
        args += ["-f", name]
    return subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=directory,
        env={"PATH": clean_env()["PATH"], "HOME": str(directory), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def compose_model(directory: Path, *files: str, **env: str) -> dict:
    result = compose_config(directory, *files, **env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_compose
def test_compose_refuses_to_run_without_a_secret_key(compose_dir):
    result = compose_config(compose_dir)
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr and "defina DJANGO_SECRET_KEY no .env" in result.stderr


@needs_compose
def test_compose_app_runs_without_debug_with_pool_and_single_access_log(compose_dir):
    key = secrets.token_urlsafe(50)
    model = compose_model(compose_dir, DJANGO_SECRET_KEY=key)
    app = model["services"]["app"]
    environment = app["environment"]
    assert environment["DJANGO_SECRET_KEY"] == key and environment["DJANGO_DEBUG"] == "0"
    assert (environment["DB_POOL"], environment["DB_POOL_MIN"], environment["DB_POOL_MAX"]) == ("1", "2", "20")
    command = " ".join(app["command"])
    assert "--workers 1" in command and "--no-access-log" in command
    assert "migrate --noinput -v 0 &&" in command  # sem texto do migrate no meio dos logs JSON
    assert "ports" not in model["services"]["db"]  # o compose completo não ocupa a 5432 do host
    # O pool pode ser desligado pelo .env/shell.
    assert compose_model(compose_dir, DJANGO_SECRET_KEY=key, DB_POOL="0")["services"]["app"]["environment"]["DB_POOL"] == "0"


@needs_compose
def test_compose_with_env_example_lines_up_credentials_and_rejects_the_example_key(compose_dir):
    shutil.copy(BASE_DIR / ".env.example", compose_dir / ".env")
    model = compose_model(compose_dir)
    app, db = model["services"]["app"]["environment"], model["services"]["db"]["environment"]
    # A chave do exemplo passa pelo compose, mas o settings recusa com DEBUG=0.
    assert app["DJANGO_SECRET_KEY"] == env_example_value("DJANGO_SECRET_KEY")
    assert is_weak_secret_key(app["DJANGO_SECRET_KEY"]) and app["DJANGO_DEBUG"] == "0"
    # Mesma senha no contêiner do banco, no app e no host (scripts/run_dev.sh lê o .env).
    assert db["POSTGRES_PASSWORD"] == app["DB_PASSWORD"] == env_example_value("DB_PASSWORD") == "postgres"
    assert app["DB_POOL"] == env_example_value("DB_POOL") == "1"


@needs_compose
def test_compose_dev_override_publishes_db_only_on_loopback(compose_dir):
    files = ("docker-compose.yml", "deploy/compose.dev.yml")
    [port] = compose_model(compose_dir, *files, DJANGO_SECRET_KEY="x")["services"]["db"]["ports"]
    assert (port["host_ip"], port["target"], str(port["published"])) == ("127.0.0.1", 5432, "5432")
    [port] = compose_model(compose_dir, *files, DJANGO_SECRET_KEY="x", DB_PUBLISH_PORT="5433")["services"]["db"]["ports"]
    assert str(port["published"]) == "5433"
    app = compose_model(compose_dir, *files, DJANGO_SECRET_KEY="x")["services"]["app"]
    assert app["environment"]["DB_HOST"] == "db" and app["environment"]["DB_PORT"] == "5432"
    assert "ports" not in app


def test_dev_db_script_starts_only_the_db_with_the_dev_override(tmp_path):
    """scripts/dev_db.sh com um `docker` falso que registra a chamada."""
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$*" "$DJANGO_SECRET_KEY" >> "$FAKE_DOCKER_CALLS"\n')
    fake.chmod(0o755)
    env = clean_env(PATH=f"{fake.parent}:{clean_env()['PATH']}", FAKE_DOCKER_CALLS=str(calls))
    for args in ([], ["stop"]):
        script = ["bash", "scripts/dev_db.sh", *args]
        result = subprocess.run(script, cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    up, stop = [line.split("|") for line in calls.read_text().splitlines()]
    assert up[0] == "compose -f docker-compose.yml -f deploy/compose.dev.yml up -d --wait db"
    assert stop[0] == "compose -f docker-compose.yml -f deploy/compose.dev.yml stop db"
    has_local_key = (BASE_DIR / ".env").is_file() and re.search(r"^DJANGO_SECRET_KEY=.+", (BASE_DIR / ".env").read_text(), re.M)
    assert up[1] or has_local_key  # o compose valida a chave do app mesmo sem subir o app


def test_uvicorn_commands_turn_off_the_duplicate_access_log():
    assert "--no-access-log" in dockerfile_cmd()
    assert "--no-access-log" in (BASE_DIR / "scripts" / "run_dev.sh").read_text()
    assert "--no-access-log" in (BASE_DIR / "docker-compose.yml").read_text()


# --- A imagem, reproduzida sem Docker ----------------------------------------------------

SERVER_APP_NAME = "fdr-ops-image"  # application_name das conexões do servidor (PGAPPNAME)
METRICS_TOKEN = "ops-metrics-token"
POOL_MAX = 4  # pequeno de propósito: vazamento de conexão viraria PoolTimeout


@pytest.fixture(scope="module")
def image_root(tmp_path_factory) -> Path:
    """/app da imagem: só o que os COPY levam, com o collectstatic do build já rodado."""
    root = tmp_path_factory.mktemp("imagem") / "app"
    root.mkdir()
    build_env: dict[str, str] = {}  # ENV vigentes até o RUN do collectstatic
    for word, rest in dockerfile_instructions():
        if word == "COPY":
            source, target = rest.split()
            if source == "requirements.txt":
                continue  # as dependências já estão instaladas neste ambiente
            destination = root / target / source if target.endswith("/") else root / target
            if (BASE_DIR / source).is_dir():
                shutil.copytree(BASE_DIR / source, destination, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(BASE_DIR / source, destination)
        elif word == "ENV":
            build_env.update(item.split("=", 1) for item in shlex.split(rest))
        elif word == "RUN" and "collectstatic" in rest:
            result = subprocess.run(
                ["sh", "-c", rest], cwd=root, env=clean_env(**build_env), capture_output=True, text=True, timeout=300
            )
            assert result.returncode == 0, result.stderr
    assert (root / "staticfiles" / "staticfiles.json").is_file()
    return root


@dataclass
class ImageServer:
    process: subprocess.Popen
    url: str
    log_path: Path
    root: Path
    _stopped: bool = field(default=False)

    def stop(self) -> list[str]:
        """Encerra (SIGTERM, como o `docker stop`) e devolve as linhas do log."""
        if not self._stopped:
            self._stopped = True
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        return [line for line in self.log_path.read_text().splitlines() if line.strip()]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def image_server(image_root, transactional_db, tmp_path):
    """O CMD da imagem com os ENV dela e o ambiente do compose (DEBUG=0, pool ligado)."""
    port = _free_port()
    cmd = dockerfile_cmd()
    assert cmd[0] == "uvicorn"
    args = cmd[1:]
    args[args.index("--host") + 1] = "127.0.0.1"
    args[args.index("--port") + 1] = str(port)
    db = connection.settings_dict
    env = clean_env(
        **dockerfile_env(),
        DJANGO_SECRET_KEY=secrets.token_urlsafe(50),
        DB_NAME=db["NAME"],
        DB_USER=db["USER"],
        DB_PASSWORD=db["PASSWORD"] or "",
        DB_HOST=db["HOST"] or "localhost",
        DB_PORT=str(db["PORT"] or "5432"),
        DB_POOL="1",
        DB_POOL_MIN="2",
        DB_POOL_MAX=str(POOL_MAX),
        SECURE_COOKIES="0",  # HTTP puro no teste; atrás do Caddy o compose força 1
        LOG_LEVEL="INFO",
        LOG_FORMAT="json",
        METRICS_TOKEN=METRICS_TOKEN,
        PGAPPNAME=SERVER_APP_NAME,
        REALTIME_PING_INTERVAL="0.5",
    )
    assert env["DJANGO_DEBUG"] == "0"
    log_path = tmp_path / "server.log"
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", *args], cwd=image_root, env=env, stdout=log_file, stderr=subprocess.STDOUT
        )
    server = ImageServer(process, f"http://127.0.0.1:{port}", log_path, image_root)
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                pytest.fail(f"o servidor saiu com código {process.returncode}:\n{log_path.read_text()}")
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{server.url}/health", timeout=1.0).status_code == 200:
                    break
            if time.monotonic() > deadline:
                pytest.fail(f"o servidor não respondeu em 30 s:\n{log_path.read_text()}")
            time.sleep(0.1)
        yield server
    finally:
        server.stop()


def parse_json_lines(lines: list[str]) -> list[dict]:
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            pytest.fail(f"linha de log fora do JSON: {line!r}")
    return records


def test_image_boots_from_copied_dirs_and_writes_only_json_logs(image_server):
    css = manifest_paths(image_server.root / "staticfiles")["css/app.css"]
    with httpx.Client(base_url=image_server.url, timeout=STEP_TIMEOUT) as client:
        assert client.get("/health").status_code == 200
        home = client.get("/")
        assert home.status_code == 200 and f"/static/{css}" in home.text  # template aponta o arquivo com hash
        assert client.get("/api/competitions").status_code == 200
        assert client.get("/nao-existe").status_code == 404

        # Estático com hash: já comprimido no build e com cache imutável.
        asset = client.get(f"/static/{css}", headers={"Accept-Encoding": "br"})
        assert asset.status_code == 200 and asset.headers["content-encoding"] == "br"
        assert asset.headers["cache-control"] == FOREVER
        assert asset.content == (image_server.root / "staticfiles" / css).read_bytes()
        assert client.get(f"/static/{css}", headers={"Accept-Encoding": "gzip"}).headers["content-encoding"] == "gzip"
        assert client.get("/static/css/app.css").headers["cache-control"] == "max-age=3600, public"

        exposition = client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}).text
    assert 'fdr_http_requests_total{method="GET",route="/",status="200"} 1' in exposition
    assert 'fdr_http_requests_total{method="GET",route="/static/*",status="200"} 3' in exposition
    assert 'fdr_http_requests_total{method="GET",route="unmatched",status="404"} 1' in exposition

    records = parse_json_lines(image_server.stop())
    loggers = [record["logger"] for record in records]
    assert "uvicorn.access" not in loggers
    assert not any("color_message" in record for record in records)
    messages = [record["msg"] for record in records if record["logger"] == "uvicorn.error"]
    assert any(message.startswith("Started server process") for message in messages)
    assert any(message.startswith("Finished server process") for message in messages)  # desligamento também
    access = [record for record in records if record["logger"] == "fdr.http"]
    # Uma linha por requisição (estáticos, /health e /metrics ficam fora do log de acesso).
    assert [record["path"] for record in access] == ["/", "/api/competitions", "/nao-existe"]
    assert [(record["route"], record["status"]) for record in access] == [
        ("/", 200),
        ("/api/competitions", 200),
        ("unmatched", 404),
    ]
    assert not [record for record in records if record["level"] in {"ERROR", "CRITICAL"}]


# --- Pool de conexões no processo ASGI -------------------------------------------------


@pytest.fixture
def ops_match(db):
    league = make_league(n_teams=4, team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"])
    home, away = league["teams"][0], league["teams"][1]
    return make_match(league["stage"], home, away, kickoff_at=timeutils.now() - timedelta(minutes=5), round=league["rounds"][0])


async def sse_events(response: httpx.Response) -> AsyncIterator[dict]:
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        *blocks, buffer = buffer.split("\n\n")
        for block in blocks:
            if not block:
                continue
            event: dict = {}
            for line in block.split("\n"):
                name, _, value = line.partition(":")
                event[name] = value[1:] if value.startswith(" ") else value
            if "data" in event:
                event["data"] = json.loads(event["data"])
            yield event


async def next_event(events: AsyncIterator[dict], wanted: Callable[[dict], bool]) -> dict:
    async def find() -> dict:
        async for event in events:
            if wanted(event):
                return event
        raise AssertionError("o stream terminou antes do evento esperado")

    return await asyncio.wait_for(find(), STEP_TIMEOUT)


def _server_db_connections() -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", [SERVER_APP_NAME])
            return cursor.fetchone()[0]
    finally:
        connection.close()


async def test_image_with_pool_handles_login_writes_sse_and_bursts(image_server, operator_user, ops_match):
    """Fluxo completo com o pool ligado: as conexões SSE não seguram conexão do pool."""
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=image_server.url, timeout=STEP_TIMEOUT, limits=limits) as client:
        token = (await client.get("/api/auth/me")).json()["csrf_token"]
        login = await client.post(
            "/api/auth/login", json={"username": "operador", "password": "senha-forte-123"}, headers={"X-CSRFToken": token}
        )
        assert login.status_code == 200, login.text
        token = login.json()["csrf_token"]

        async with contextlib.AsyncExitStack() as stack:
            streams = []
            for _ in range(1 + 4 * POOL_MAX):  # bem mais conexões SSE que o tamanho do pool
                response = await stack.enter_async_context(client.stream("GET", "/api/stream"))
                assert response.status_code == 200
                events = sse_events(response)
                await next_event(events, lambda event: event.get("event") == "ping")  # já inscrito no hub
                streams.append(events)

            async def write() -> httpx.Response:
                return await client.post(
                    f"/api/ops/matches/{ops_match.id}/events",
                    json={"type": "match_start"},
                    headers={"X-CSRFToken": token, "Idempotency-Key": "ops-pool-1"},
                )

            reads = [client.get(path) for path in ("/api/home", "/api/competitions", f"/api/matches/{ops_match.id}") * 15]
            posted, *responses = await asyncio.gather(write(), *reads)
            assert posted.status_code == 201, posted.text
            assert [response.status_code for response in responses] == [200] * len(reads)

            for events in streams:
                message = await next_event(events, lambda event: event.get("event") == "match")
                assert message["data"]["match"]["id"] == ops_match.id

            connections = await asyncio.to_thread(_server_db_connections)
            assert 2 <= connections <= POOL_MAX  # conexões do pool (min_size=2) reaproveitadas

    records = parse_json_lines(image_server.stop())
    assert not [record for record in records if record["level"] in {"ERROR", "CRITICAL"}]
    assert not any("PoolTimeout" in json.dumps(record) for record in records)
    statuses = [record["status"] for record in records if record["logger"] == "fdr.http" and record["method"] == "POST"]
    assert statuses == [200, 201]  # login e lançamento


@needs_compose
def test_compose_proxy_uses_the_internal_ca_by_default_and_lets_encrypt_with_an_email(compose_dir):
    proxy = compose_model(compose_dir, DJANGO_SECRET_KEY="x")["services"]["proxy"]
    assert proxy["environment"]["CADDY_TLS"] == "internal"
    proxy = compose_model(compose_dir, DJANGO_SECRET_KEY="x", CADDY_TLS="eu@exemplo.com")["services"]["proxy"]
    assert proxy["environment"]["CADDY_TLS"] == "eu@exemplo.com"
    assert "tls {$CADDY_TLS:internal}" in (BASE_DIR / "deploy" / "Caddyfile").read_text()


def _fake_docker_project(tmp_path, script, body):
    """Cópia de scripts/<script> num projeto vazio + um `docker` falso (registra cada chamada)."""
    (tmp_path / "proj" / "scripts").mkdir(parents=True)
    shutil.copy(BASE_DIR / "scripts" / script, tmp_path / "proj" / "scripts")
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER_CALLS"\n' + body)
    fake.chmod(0o755)
    calls = tmp_path / "calls.txt"
    env = clean_env(PATH=f"{fake.parent}:{clean_env()['PATH']}", FAKE_DOCKER_CALLS=str(calls))
    return tmp_path / "proj", env, calls


def test_backup_script_dumps_the_db_and_keeps_the_latest(tmp_path):
    body = 'case "$*" in *pg_dump*) printf DUMP;; *"test -n"*) exit 1;; esac\n'  # sem escudos
    proj, env, calls = _fake_docker_project(tmp_path, "backup.sh", body)
    (proj / "backups").mkdir()
    for old in ("fdr-2020-01-01_0000.dump", "fdr-2020-01-02_0000.dump"):
        (proj / "backups" / old).write_text("velho")
    result = subprocess.run(["bash", "scripts/backup.sh"], cwd=proj, env={**env, "KEEP": "2"}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    dumps = sorted(p.name for p in (proj / "backups").glob("fdr-*.dump"))
    assert len(dumps) == 2 and dumps[0] == "fdr-2020-01-02_0000.dump"  # o mais antigo saiu
    assert (proj / "backups" / dumps[1]).read_text() == "DUMP"
    assert not list((proj / "backups").glob("media-*"))
    assert "pg_dump" in calls.read_text()


@pytest.mark.parametrize("fails", [False, True])
def test_restore_script_stops_the_app_and_restores_in_one_transaction(tmp_path, fails):
    body = 'cat > /dev/null\n' + ('case "$*" in *pg_restore*) exit 1;; esac\n' if fails else "")
    proj, env, calls = _fake_docker_project(tmp_path, "restore.sh", body)
    (proj / "fdr.dump").write_text("DUMP")
    (proj / "media.tgz").write_text("TGZ")
    script = ["bash", "scripts/restore.sh", "fdr.dump", "media.tgz"]
    refused = subprocess.run(script, cwd=proj, env=env, input="n\n", capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0 and not calls.exists()  # sem confirmação, nada acontece
    result = subprocess.run(script, cwd=proj, env={**env, "FORCE": "1"}, capture_output=True, text=True, timeout=30)
    lines = calls.read_text().splitlines()
    assert lines[:2] == ["compose up -d --wait db", "compose stop app proxy"]
    assert "pg_restore" in lines[2] and "--single-transaction" in lines[2] and "--exit-on-error" in lines[2]
    if fails:
        assert result.returncode == 1 and lines[3:] == ["compose up -d"]  # volta com os dados de antes
    else:
        assert result.returncode == 0, result.stderr
        assert lines[3] == "compose up -d --wait" and lines[4].startswith("compose exec -T app tar xzf")
