import hmac

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden



def metrics(request):  # noqa: F811 - nome da rota
    """Métricas no formato Prometheus.

    Acesso: header `Authorization: Bearer <METRICS_TOKEN>` (quando configurado)
    ou usuário logado com a permissão `observability.view_metrics`.
    """
    from .metrics import metrics as registry

    token = settings.METRICS_TOKEN
    header = request.headers.get("Authorization", "")
    allowed = bool(token) and hmac.compare_digest(header, f"Bearer {token}")
    user = getattr(request, "user", None)
    if not allowed and user is not None and user.is_authenticated and user.has_perm("observability.view_metrics"):
        allowed = True
    if not allowed:
        return HttpResponseForbidden("forbidden\n", content_type="text/plain")
    try:
        from realtime.hub import hub

        registry.set_gauge("fdr_sse_connections", hub.subscriber_count)
    except Exception:  # pragma: no cover - hub opcional
        pass
    return HttpResponse(registry.render(), content_type="text/plain; version=0.0.4; charset=utf-8")
