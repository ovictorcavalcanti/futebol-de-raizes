#!/usr/bin/env bash
# Backup do servidor (docker compose): banco e escudos enviados pelo admin.
#
#   scripts/backup.sh               # grava em backups/ e guarda os 14 mais recentes
#   KEEP=30 scripts/backup.sh       # guarda outra quantidade
#
# Gera backups/fdr-AAAA-MM-DD_HHMM.dump (pg_dump -Fc) e, se houver escudos,
# backups/media-AAAA-MM-DD_HHMM.tgz. Para restaurar: scripts/restore.sh.
# Diário no cron (crontab -e), por exemplo às 4h:
#   0 4 * * * cd /caminho/futebol-de-raizes && scripts/backup.sh >> backups/cron.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups
stamp=$(date +%Y-%m-%d_%H%M)
keep=${KEEP:-14}

# Usuário e banco vêm do próprio contêiner (POSTGRES_USER/POSTGRES_DB do compose).
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -d "$POSTGRES_DB"' > "backups/fdr-$stamp.dump.part"
mv "backups/fdr-$stamp.dump.part" "backups/fdr-$stamp.dump"
echo "banco: backups/fdr-$stamp.dump"

if docker compose exec -T app sh -c 'test -n "$(ls -A /app/media 2>/dev/null)"'; then
  docker compose exec -T app tar czf - -C /app media > "backups/media-$stamp.tgz"
  echo "escudos: backups/media-$stamp.tgz"
fi

# Rotação: mantém os $keep mais recentes de cada tipo.
for pattern in 'fdr-*.dump' 'media-*.tgz'; do
  find backups -maxdepth 1 -name "$pattern" | sort -r | tail -n +"$((keep + 1))" | xargs -r rm -f
done
