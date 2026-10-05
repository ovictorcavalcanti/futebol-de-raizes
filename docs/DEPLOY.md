# Colocar no ar (VPS) levando os dados do PC

Passo a passo para subir o Futebol de Raízes num VPS (Droplet da DigitalOcean, Ubuntu)
com o `docker-compose.yml` do repositório, levando os dados que já estão no PC (jogos,
times, temporadas, usuários, lances) e os escudos enviados pelo admin.

No servidor rodam três contêineres: o PostgreSQL 16, o app (um processo uvicorn) e o
Caddy, que serve HTTPS com certificado do Let's Encrypt (tirado e renovado sozinho).

Sem domínio próprio, o endereço é o IP com traços + `.sslip.io` (ex.: o IP
`203.0.113.5` vira `203-0-113-5.sslip.io`): um serviço gratuito de DNS que responde com
o próprio IP, o que basta para o Let's Encrypt. Quando tiver um domínio, veja
[Trocar para um domínio próprio](#trocar-para-um-domínio-próprio).

## 1. Criar o servidor

1. No Mac, crie uma chave SSH se ainda não tiver (`ls ~/.ssh/id_ed25519.pub`):

   ```bash
   ssh-keygen -t ed25519 -C "seu@email.com"
   cat ~/.ssh/id_ed25519.pub        # copie a linha inteira
   ```

2. Na DigitalOcean: **Create → Droplets**.
   - Imagem: **Ubuntu 24.04 (LTS) x64**.
   - Plano: **Basic, Regular, 2 GB RAM / 1 CPU** (com 1 GB a montagem da imagem pode
     faltar memória).
   - Região: a mais próxima do público (não há região no Brasil; **New York** ou
     **Toronto** costumam ter a menor latência daqui).
   - Autenticação: **SSH Key** → **New SSH Key** → cole a linha copiada.
   - Crie e anote o **IP** do Droplet.

3. Entre no servidor e prepare o básico (firewall + Docker):

   ```bash
   ssh root@SEU_IP

   apt update && apt -y upgrade
   ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
   ufw --force enable
   curl -fsSL https://get.docker.com | sh
   docker compose version             # confere o Compose v2
   ```

## 2. Baixar o projeto e configurar

```bash
cd /root
git clone https://github.com/ovictorcavalcanti/futebol-de-raizes.git
cd futebol-de-raizes
cp .env.example .env
```

Repositório privado: o `git clone` pede usuário e senha. Use o seu usuário do GitHub e,
como senha, um token (GitHub → Settings → Developer settings → Personal access tokens →
Fine-grained, só este repositório, permissão **Contents: Read-only**).

Gere a chave secreta e a senha do banco:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(50))'   # DJANGO_SECRET_KEY
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'   # DB_PASSWORD
```

Edite o `.env` (`nano .env`) e troque estas linhas (exemplo com o IP `203.0.113.5`):

```ini
DJANGO_SECRET_KEY=<a chave gerada>
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=203-0-113-5.sslip.io,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://203-0-113-5.sslip.io
DB_PASSWORD=<a senha gerada>
SITE_ADDRESS=203-0-113-5.sslip.io
CADDY_TLS=seu@email.com
```

- Mantenha `127.0.0.1` em `DJANGO_ALLOWED_HOSTS`: o healthcheck do contêiner usa esse
  endereço.
- `CADDY_TLS` com um e-mail liga o certificado do Let's Encrypt (o e-mail recebe avisos
  de expiração, se a renovação falhar). Com `internal`, o certificado é o de teste e o
  navegador reclama.
- A `DB_PASSWORD` vale na criação do banco: trocá-la depois exige trocar também a senha
  dentro do Postgres.

Monte a imagem (alguns minutos na primeira vez):

```bash
docker compose build
```

## 3. Levar os dados do Mac

No **Mac**, na pasta do projeto, com o código igual ao do servidor (`git pull` no
`main` nos dois) e o banco migrado (`python manage.py migrate`):

```bash
set -a; . ./.env; set +a                       # DB_NAME, DB_USER do seu .env
export PGPASSWORD="$DB_PASSWORD"

# confira a versão do Postgres do Mac: tem de ser 16 (é a do servidor)
psql -h localhost -U "$DB_USER" -d "$DB_NAME" -Atc 'show server_version'

pg_dump -h localhost -U "$DB_USER" -Fc -d "$DB_NAME" -f fdr.dump

# escudos enviados pelo admin (só se a pasta media/ existir e tiver arquivos)
COPYFILE_DISABLE=1 tar czf media.tgz media

scp fdr.dump media.tgz root@SEU_IP:/root/futebol-de-raizes/
```

- Com o Postgres.app, se `pg_dump` não for encontrado, use o caminho completo:
  `/Applications/Postgres.app/Contents/Versions/latest/bin/pg_dump`.
- Se a versão do Postgres do Mac não for 16, o servidor (16) pode recusar o arquivo:
  nesse caso ajuste a imagem `postgres:16` do `docker-compose.yml` para a mesma versão
  **antes** de subir o banco pela primeira vez.
- `COPYFILE_DISABLE=1` evita que o `tar` do macOS inclua arquivos `._*` de metadados.

## 4. Restaurar no servidor e subir

No **servidor**:

```bash
cd /root/futebol-de-raizes
scripts/restore.sh fdr.dump media.tgz      # sem escudos: scripts/restore.sh fdr.dump
```

O script pede confirmação, sobe o banco, restaura tudo numa transação só (com qualquer
erro, nada muda), aplica as migrações que faltarem e sobe o app e o Caddy.

Abra `https://203-0-113-5.sslip.io` (o seu): os jogos devem estar lá. O admin fica em
`/admin/`, com os mesmos usuários e senhas do Mac.

Se a página não abrir com HTTPS, veja o log do Caddy (o certificado sai em segundos;
erro de "challenge" quase sempre é porta 80/443 fechada ou endereço errado):

```bash
docker compose logs --tail 50 proxy
docker compose ps                           # os três "running"/"healthy"
```

**Não rode `python manage.py seed` no servidor**: com `--reset` ele apaga todos os dados.

A partir daqui, cadastre só no servidor. Para refazer a migração do zero (ex.: continuou
cadastrando no Mac), gere outro `fdr.dump` e rode o `scripts/restore.sh` de novo: ele
substitui os dados do servidor.

## 5. Backup diário

```bash
scripts/backup.sh                            # testa: grava em backups/
crontab -e                                   # e acrescente a linha:
0 4 * * * cd /root/futebol-de-raizes && scripts/backup.sh >> backups/cron.log 2>&1
```

Guarda os 14 backups mais recentes (`KEEP=30 scripts/backup.sh` para mudar). Copie de vez
em quando para fora do servidor, do Mac:

```bash
scp -r root@SEU_IP:/root/futebol-de-raizes/backups ./backups-servidor
```

Os snapshots da DigitalOcean (Droplet → Backups) são um segundo nível de proteção.

Para voltar um backup: `scripts/restore.sh backups/fdr-AAAA-MM-DD_HHMM.dump
backups/media-AAAA-MM-DD_HHMM.tgz`.

## 6. Atualizar o código

```bash
cd /root/futebol-de-raizes
scripts/backup.sh                            # por garantia
git pull
docker compose up -d --build                 # monta de novo, migra e reinicia
```

O stream ao vivo cai por alguns segundos na reinicialização; os navegadores reconectam
sozinhos.

## Trocar para um domínio próprio

1. No painel do domínio, crie um registro **A** apontando para o IP do Droplet (e, se
   quiser, outro para `www`).
2. No `.env`, troque `SITE_ADDRESS`, `DJANGO_ALLOWED_HOSTS` (mantendo `127.0.0.1`) e
   `DJANGO_CSRF_TRUSTED_ORIGINS` para o domínio novo.
3. `docker compose up -d`: o Caddy tira o certificado do domínio novo sozinho.
4. Com tudo funcionando, dá para ligar o HSTS: `SECURE_HSTS_SECONDS=31536000` no `.env`
   e `docker compose up -d`.
