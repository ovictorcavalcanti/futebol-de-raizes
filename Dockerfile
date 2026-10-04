# Futebol de Raízes: UM processo ASGI (uvicorn --workers 1) serve API, páginas,
# Django Admin e o stream SSE. O hub do tempo real vive nesse processo; mais de
# um worker dividiria as conexões entre hubs e multiplicaria o polling.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Usuário sem privilégios; o código fica somente leitura para ele.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

# Dependências primeiro: esta camada só é refeita quando requirements.txt muda.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Código em lista explícita: .env, .git, testes e caches nunca entram na imagem.
COPY manage.py ./
COPY config ./config
COPY core ./core
COPY accounts ./accounts
COPY competitions ./competitions
COPY matches ./matches
COPY standings ./standings
COPY realtime ./realtime
COPY observability ./observability
COPY public_api ./public_api
COPY api ./api
COPY templates ./templates
COPY static ./static

# Estáticos com hash no nome (cache longo e imutável) e já comprimidos (.gz e
# .br), servidos pelo WhiteNoise. Com DEBUG=0 o settings exige uma chave forte:
# o collectstatic recebe uma aleatória, gerada aqui e descartada (variável só
# deste RUN, não fica na imagem). Em execução a chave vem do ambiente.
RUN install -d -o app -g app /app/staticfiles /app/media
USER app
RUN DJANGO_DEBUG=0 \
    DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')" \
    python manage.py collectstatic --noinput

# A imagem roda em modo produção; DJANGO_SECRET_KEY é obrigatória no
# `docker run -e ...` ou no compose (sem ela o processo não sobe).
ENV DJANGO_DEBUG=0

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# --timeout-graceful-shutdown: as conexões SSE nunca terminam sozinhas; no
# desligamento o uvicorn as encerra após 5 s e os navegadores reconectam.
# --no-access-log: o acesso já sai em JSON pelo log `fdr.http` (uma linha por
# requisição); o do uvicorn seria uma segunda linha para a mesma requisição.
CMD ["uvicorn", "config.asgi:application", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "5", "--no-access-log"]
