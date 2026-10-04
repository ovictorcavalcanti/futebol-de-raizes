#!/usr/bin/env bash
# Desenvolvimento sem PostgreSQL local: sobe só o serviço db do compose,
# publicado em 127.0.0.1:${DB_PUBLISH_PORT:-5432} (deploy/compose.dev.yml).
#
#   scripts/dev_db.sh               # sobe e espera ficar saudável
#   scripts/dev_db.sh stop          # para o banco (os dados ficam no volume pgdata)
#
# No .env: DB_HOST=localhost e DB_PASSWORD=postgres. Com a 5432 ocupada, use
# DB_PUBLISH_PORT=5433 e DB_PORT=5433. Depois: scripts/run_dev.sh ou pytest.
set -euo pipefail

cd "$(dirname "$0")/.."

# O compose valida o arquivo inteiro, inclusive a chave obrigatória do app, que
# aqui não sobe: sem DJANGO_SECRET_KEY no shell nem no .env, passa uma de mentira.
if [[ -z "${DJANGO_SECRET_KEY:-}" ]] && ! grep -Eqs '^DJANGO_SECRET_KEY=.+' .env; then
  export DJANGO_SECRET_KEY=nao-usada-aqui-so-o-banco-sobe
fi

compose=(docker compose -f docker-compose.yml -f deploy/compose.dev.yml)
if [[ "${1:-up}" == "stop" ]]; then
  exec "${compose[@]}" stop db
fi
exec "${compose[@]}" up -d --wait db
