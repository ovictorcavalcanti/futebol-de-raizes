"""Configuração do Futebol de Raízes.

Tudo que varia por ambiente vem de variáveis de ambiente (ver `.env.example`).
"""

import os
from pathlib import Path

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


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-futebol-de-raizes-troque-em-producao")
DEBUG = env_bool("DJANGO_DEBUG", True)
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
# que evita abrir uma conexão nova a cada requisição no processo ASGI.
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
WHITENOISE_MAX_AGE = 60 if DEBUG else 31536000
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
LOGIN_URL = "/admin/login/"

# --- Cache (limite de uso da API pública) --------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fdr",
    }
}

# --- Marca ------------------------------------------------------------------
# Configuração do logo: troque por variáveis de ambiente, sem mexer no código.
# BRAND_LOGO_URL e BRAND_LOGO_DARK_URL aceitam caminho estático ("img/logo.svg")
# ou URL absoluta. Sem BRAND_LOGO_DARK_URL, o tema escuro usa o mesmo logo.
BRAND = {
    "name": env("BRAND_NAME", "Futebol de Raízes"),
    "short_name": env("BRAND_SHORT_NAME", "Raízes"),
    "tagline": env("BRAND_TAGLINE", "O futebol pernambucano, lance a lance"),
    "logo_url": env("BRAND_LOGO_URL", "img/logo.svg"),
    "logo_dark_url": env("BRAND_LOGO_DARK_URL", ""),
    "logo_alt": env("BRAND_LOGO_ALT", "Futebol de Raízes"),
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
}

# --- Observabilidade --------------------------------------------------------
METRICS_TOKEN = env("METRICS_TOKEN", "")
LOG_LEVEL = env("LOG_LEVEL", "INFO")
LOG_FORMAT = env("LOG_FORMAT", "json")
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
    },
}

# --- API pública -------------------------------------------------------------
PUBLIC_API = {
    "DEFAULT_RATE_LIMIT_PER_MINUTE": int(env("PUBLIC_API_RATE_LIMIT", "60")),
    "CACHE_MAX_AGE": int(env("PUBLIC_API_CACHE_MAX_AGE", "10")),
}
