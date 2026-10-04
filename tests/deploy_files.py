"""Leitura dos arquivos de deploy (Dockerfile, .env.example) e ambiente limpo para subprocessos.

Os testes de operação usam o que está escrito nos arquivos, não uma cópia: se o
Dockerfile mudar o comando, os testes rodam o comando novo.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Variáveis lidas pelo config/settings.py que não podem vazar do shell para os subprocessos.
_INHERITED_PREFIXES = ("DJANGO_", "DB_", "STATICFILES_", "LOG_", "SECURE_COOKIES", "REALTIME_", "METRICS_", "PG")


def clean_env(**overrides: str | None) -> dict[str, str]:
    """os.environ sem a configuração do app (como num contêiner novo) + `overrides` (None = ausente)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(_INHERITED_PREFIXES)}
    env.pop("PYTHONPATH", None)
    for key, value in overrides.items():
        if value is not None:
            env[key] = value
    return env


def env_example_value(name: str) -> str:
    """Valor de uma variável no .env.example (o que `cp .env.example .env` entrega)."""
    for line in (BASE_DIR / ".env.example").read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError(f"{name} ausente do .env.example")


def dockerfile_instructions() -> list[tuple[str, str]]:
    """(INSTRUÇÃO, argumentos) do Dockerfile, com as continuações de linha unidas."""
    text = re.sub(r"\\\n", " ", (BASE_DIR / "Dockerfile").read_text())
    instructions = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            instructions.append((word.upper(), rest.strip()))
    return instructions


def dockerfile_env() -> dict[str, str]:
    """Variáveis dos `ENV` da imagem (formato chave=valor)."""
    env: dict[str, str] = {}
    for word, rest in dockerfile_instructions():
        if word == "ENV":
            for item in shlex.split(rest):
                key, _, value = item.partition("=")
                env[key] = value
    return env


def dockerfile_run(containing: str) -> str:
    """O único `RUN` que contém `containing`."""
    [command] = [rest for word, rest in dockerfile_instructions() if word == "RUN" and containing in rest]
    return command


def dockerfile_cmd() -> list[str]:
    """O `CMD` da imagem (forma JSON)."""
    [command] = [rest for word, rest in dockerfile_instructions() if word == "CMD"]
    return json.loads(command)
