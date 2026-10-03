"""API pública (fase 12) — preenchida pelo módulo public_api."""

from ninja import NinjaAPI

public_api = NinjaAPI(
    title="Futebol de Raízes · API pública",
    version="1.0.0",
    urls_namespace="public_v1",
    docs_url="/docs",
)
