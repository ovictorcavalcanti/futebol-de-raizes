#!/usr/bin/env bash
# Restaura no servidor (docker compose) um backup do banco e, opcionalmente, os escudos.
#
#   scripts/restore.sh fdr.dump              # só o banco
#   scripts/restore.sh fdr.dump media.tgz    # banco + escudos enviados pelo admin
#
# O dump é o do pg_dump -Fc (scripts/backup.sh, ou do PC: veja docs/DEPLOY.md);
# o .tgz tem a pasta media/ na raiz. ATENÇÃO: substitui TODOS os dados do banco
# do servidor pelos do arquivo. O app fica parado durante a restauração.
set -euo pipefail

cd "$(dirname "$0")/.."
dump=${1:?uso: scripts/restore.sh arquivo.dump [media.tgz]}
media=${2:-}
[[ -f "$dump" ]] || { echo "arquivo não encontrado: $dump" >&2; exit 1; }
[[ -z "$media" || -f "$media" ]] || { echo "arquivo não encontrado: $media" >&2; exit 1; }

if [[ "${FORCE:-}" != "1" ]]; then
  read -r -p "Isto apaga os dados atuais do servidor e põe os de $dump. Continuar? [s/N] " answer
  [[ "$answer" == [sS] ]] || { echo "cancelado"; exit 1; }
fi

docker compose up -d --wait db
docker compose stop app proxy 2>/dev/null || true

# --clean --if-exists: apaga o que existe antes de recriar; --no-owner/--no-privileges:
# o dono passa a ser o usuário do banco do servidor (no PC pode ser outro). Numa
# transação só: com qualquer erro, nada muda e o app volta com os dados de antes.
if ! docker compose exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --single-transaction --exit-on-error' < "$dump"; then
  echo "a restauração falhou: o banco ficou como estava" >&2
  docker compose up -d
  exit 1
fi
echo "banco restaurado"

# O app aplica as migrações que faltarem ao subir.
docker compose up -d --wait

if [[ -n "$media" ]]; then
  docker compose exec -T app tar xzf - -C /app --no-same-owner < "$media"
  echo "escudos restaurados"
fi
echo "pronto"
