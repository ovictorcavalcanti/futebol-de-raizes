"""Configuração do Futebol de Raízes.

Tudo que varia por ambiente vem de variáveis de ambiente (ver `.env.example`).
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# Chave só de desenvolvimento. Ela e o exemplo do .env.example são públicas (estão no
# repositório) e assinam sessão, CSRF e redefinição de senha: com DEBUG=0 o processo
# se recusa a subir com elas ou com qualquer chave fraca.
DEV_SECRET_KEY = "dev-insecure-futebol-de-raizes-troque-em-producao"
PUBLIC_SECRET_KEYS = frozenset({DEV_SECRET_KEY, "troque-por-uma-chave-longa-e-aleatoria"})


def is_weak_secret_key(key):
    """Chave pública ou fraca pela regra do `check --deploy` (security.W009)."""
    return key in PUBLIC_SECRET_KEYS or len(key) < 50 or len(set(key)) < 5 or key.startswith("django-insecure-")


SECRET_KEY = env("DJANGO_SECRET_KEY") or DEV_SECRET_KEY
DEBUG = env_bool("DJANGO_DEBUG", True)
if not DEBUG and is_weak_secret_key(SECRET_KEY):
    raise ImproperlyConfigured(
        "Defina DJANGO_SECRET_KEY (50+ caracteres aleatórios) fora do desenvolvimento: a chave atual "
        "está vazia, é fraca ou é pública (padrão do código ou do .env.example). Gere uma com "
        "python -c 'import secrets; print(secrets.token_urlsafe(50))'"
    )
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "https://localhost,http://localhost:8000")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "core",
    "accounts",
    "competitions",
    "matches",
    "standings",
    "realtime",
    "observability",
    "public_api",
    "api",
]

MIDDLEWARE = [
    "observability.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.ratelimit.RateLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.brand",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Banco -----------------------------------------------------------------
# Um único PostgreSQL. Com DB_POOL=1 usa o pool do psycopg (Django 5.1+),
# que evita abrir uma conexão nova a cada requisição no processo ASGI. Desligado
# por padrão (pytest e desenvolvimento); o docker-compose.yml liga. O hub do
# tempo real segura uma conexão do pool o tempo todo: DB_POOL_MAX >= 3. O pool
# exige CONN_MAX_AGE=0.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "fdr"),
        "USER": env("DB_USER", "postgres"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {},
        "TEST": {"NAME": env("TEST_DB_NAME", "test_fdr")},
    }
}
if env_bool("DB_POOL", False):
    DATABASES["default"]["OPTIONS"]["pool"] = {
        "min_size": int(env("DB_POOL_MIN", "2")),
        "max_size": int(env("DB_POOL_MAX", "20")),
        "timeout": 10,
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
# Backend com bloqueio progressivo de login (accounts/throttle.py): vale para a
# API e para o Django Admin.
AUTHENTICATION_BACKENDS = ["accounts.backends.ThrottledModelBackend"]
LOGIN_THROTTLE = {
    "ENABLED": env_bool("LOGIN_THROTTLE_ENABLED", True),
    "USER_FAILURES": int(env("LOGIN_THROTTLE_USER_FAILURES", "5")),  # falhas seguidas por usuário + IP
    "BASE_LOCK": int(env("LOGIN_THROTTLE_BASE_LOCK", "60")),  # 1º bloqueio (s); dobra a cada novo
    "MAX_LOCK": int(env("LOGIN_THROTTLE_MAX_LOCK", "900")),  # teto do bloqueio (s)
    "STRIKE_MEMORY": int(env("LOGIN_THROTTLE_STRIKE_MEMORY", "86400")),  # memória dos bloqueios (s)
    "IP_FAILURES": int(env("LOGIN_THROTTLE_IP_FAILURES", "20")),  # falhas por IP, qualquer usuário...
    "IP_WINDOW": int(env("LOGIN_THROTTLE_IP_WINDOW", "900")),  # ...nesta janela (s)
    "IP_LOCK": int(env("LOGIN_THROTTLE_IP_LOCK", "900")),  # bloqueio do IP (s)
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Idioma e fuso ------------------------------------------------------------
# O banco guarda tudo em UTC; o "dia", o relógio e os horários exibidos seguem
# o fuso abaixo (horário de Brasília), configurado só aqui.
LANGUAGE_CODE = "pt-br"
TIME_ZONE = env("APP_TIME_ZONE", "America/Sao_Paulo")
USE_I18N = True
USE_TZ = True

# --- Estáticos --------------------------------------------------------------
STATIC_URL = "/static/"
# Arquivos enviados (escudos). Servidos pelo próprio app em /media/ (poucos e pequenos).
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT") or str(BASE_DIR / "media"))
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": env(
            "STATICFILES_BACKEND",
            "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG else "core.storage.ModuleManifestStaticFilesStorage",
        )
    },
}
# Arquivos com hash no nome já saem com cache de 10 anos + immutable (WhiteNoise);
# este prazo vale só para URLs sem hash, que podem mudar sem trocar de nome.
WHITENOISE_MAX_AGE = 60 if DEBUG else 3600
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG

# --- Sessão e CSRF ------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # o front lê o token do cookie para enviar no header
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", not DEBUG)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# HSTS e redirecionamento para HTTPS: desligados por padrão. No compose o Caddy já
# redireciona http:// para https://, e HSTS com o certificado da CA interna (teste,
# localhost) prenderia o navegador a ele. Em produção com certificado público, ligue
# SECURE_HSTS_SECONDS (ex.: 31536000). O /health fica fora do redirecionamento (o
# healthcheck do contêiner fala HTTP direto com o uvicorn).
SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "0") or 0)
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
SECURE_REDIRECT_EXEMPT = [r"^health$"]
LOGIN_URL = "/admin/login/"

# --- Limites de acesso -------------------------------------------------------
# Requisições por IP por minuto nas rotas /api/ (core/ratelimit.py); 0 desliga.
API_RATE_LIMIT_PER_MINUTE = int(env("API_RATE_LIMIT_PER_MINUTE", "240"))
# Corpo máximo de uma requisição (os JSON da API têm poucos KB).
DATA_UPLOAD_MAX_MEMORY_SIZE = int(env("DATA_UPLOAD_MAX_MEMORY_SIZE", str(1024 * 1024)))

# --- Cache --------------------------------------------------------------------
# "default": limite de uso da API pública. "reads": micro-cache das leituras mais
# quentes (GET /api/home e /api/competitions/{slug}), separado para que as chaves
# do limite de uso nunca sejam despejadas por ele.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fdr",
    },
    "reads": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fdr-reads",
        "OPTIONS": {"MAX_ENTRIES": 500},
    },
}
# Validade (s) do micro-cache das leituras, chaveado pelo cursor do outbox: toda
# escrita que publica mensagem já invalida. Edição no admin que não publica nada
# (nome de time/competição, escudo) aparece em até este tempo. 0 desliga.
READ_CACHE_SECONDS = float(env("READ_CACHE_SECONDS", "5"))

# --- Marca ------------------------------------------------------------------
# Configuração do logo: troque por variáveis de ambiente, sem mexer no código.
# BRAND_LOGO_URL e BRAND_LOGO_DARK_URL aceitam caminho estático ("img/logo.svg")
# ou URL absoluta. Com BRAND_LOGO_DARK_URL vazio: o logo padrão usa a versão para
# fundo escuro (img/logo-dark.svg); um logo próprio se repete no tema escuro.
DEFAULT_BRAND_LOGO = "img/logo.svg"
BRAND_LOGO = env("BRAND_LOGO_URL") or DEFAULT_BRAND_LOGO
BRAND_NAME = env("BRAND_NAME", "Futebol de Raízes")  # também é o alt do logo e o título do admin
BRAND = {
    "name": BRAND_NAME,
    "short_name": env("BRAND_SHORT_NAME", "Raízes"),
    "tagline": env("BRAND_TAGLINE", "O futebol pernambucano, lance a lance"),
    "logo_url": BRAND_LOGO,
    "logo_dark_url": env("BRAND_LOGO_DARK_URL") or ("img/logo-dark.svg" if BRAND_LOGO == DEFAULT_BRAND_LOGO else ""),
    "logo_alt": env("BRAND_LOGO_ALT") or BRAND_NAME,
    "favicon_url": env("BRAND_FAVICON_URL", "img/favicon.svg"),
    "theme_color": env("BRAND_THEME_COLOR", "#12306B"),
}

# --- Tempo real -------------------------------------------------------------
REALTIME = {
    "PING_INTERVAL": float(env("REALTIME_PING_INTERVAL", "20")),
    "POLL_INTERVAL": float(env("REALTIME_POLL_INTERVAL", "2")),
    "OUTBOX_RETENTION_HOURS": int(env("OUTBOX_RETENTION_HOURS", "24")),
    "SUBSCRIBER_QUEUE_SIZE": int(env("REALTIME_QUEUE_SIZE", "1000")),
    "HUB_ENABLED": env_bool("REALTIME_HUB_ENABLED", True),
    # Conexões SSE abertas: por IP (várias abas e NAT cabem) e no processo.
    "MAX_STREAMS_PER_IP": int(env("REALTIME_MAX_STREAMS_PER_IP", "20")),
    "MAX_STREAMS": int(env("REALTIME_MAX_STREAMS", "5000")),
}

# --- Observabilidade --------------------------------------------------------
METRICS_TOKEN = env("METRICS_TOKEN", "")
LOG_LEVEL = env("LOG_LEVEL", "INFO")
LOG_FORMAT = env("LOG_FORMAT", "json")
LOGGING_CONFIG = "observability.logging.configure"  # dictConfig + avisos (warnings) no mesmo formato
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "observability.logging.JsonFormatter"},
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "filters": {"request_context": {"()": "observability.logging.RequestContextFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT if LOG_FORMAT in {"json", "plain"} else "json",
            "filters": ["request_context"],
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "fdr": {"level": LOG_LEVEL, "propagate": True},
        # Sem os handlers de texto do DEFAULT_LOGGING do Django (ativos com DEBUG=1):
        # tudo sai uma vez só, pelo console acima.
        "django": {"handlers": [], "level": LOG_LEVEL, "propagate": True},
        "py.warnings": {"handlers": [], "propagate": True},
        # Uvicorn (configurado antes de o Django carregar): ciclo de vida e erros no
        # mesmo formato. "uvicorn.access" fica aqui, mudo: o dictConfig reinicia os
        # filhos não listados de um logger configurado e o uvicorn voltaria a escrever
        # o acesso em duplicata (o fdr.http já registra cada requisição).
        "uvicorn": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "uvicorn.error": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
    },
}

# --- API pública -------------------------------------------------------------
PUBLIC_API = {
    "DEFAULT_RATE_LIMIT_PER_MINUTE": int(env("PUBLIC_API_RATE_LIMIT", "60")),
    "CACHE_MAX_AGE": int(env("PUBLIC_API_CACHE_MAX_AGE", "10")),
}
