#!/usr/bin/env bash
# Desenvolvimento: migra o banco e sobe UM processo ASGI com recarga automática.
#
#   scripts/run_dev.sh              # http://127.0.0.1:8000
#   HOST=0.0.0.0 PORT=9000 scripts/run_dev.sh
#
# Um processo só, como em produção: o hub do stream SSE vive nele. Lê o .env
# da raiz, se existir (valores com espaço entre aspas duplas). Sem PostgreSQL
# local, suba o do compose antes: scripts/dev_db.sh.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

python manage.py migrate --noinput

# --reload já roda um único processo servidor (sem --workers). Com conexões SSE
# abertas, a recarga esperaria para sempre: --timeout-graceful-shutdown as
# encerra em 2 s e o navegador reconecta sozinho com Last-Event-ID.
# --no-access-log: o acesso já sai pelo log fdr.http (uma linha por requisição).
# Só o processo que vigia os arquivos (--reload) escreve umas linhas em texto
# antes de o Django carregar; o resto segue LOG_FORMAT.
exec python -m uvicorn config.asgi:application \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8000}" \
  --reload \
  --reload-dir . \
  --timeout-graceful-shutdown 2 \
  --no-access-log
