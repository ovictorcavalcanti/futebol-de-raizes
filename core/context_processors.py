from django.conf import settings
from django.templatetags.static import static


def _resolve(url: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://", "/", "data:")):
        return url
    return static(url)


def brand(request):
    """Expõe a configuração da marca (logo, nome) para todas as páginas."""
    cfg = settings.BRAND
    logo = _resolve(cfg["logo_url"])
    return {
        "brand": {
            **cfg,
            "logo_src": logo,
            "logo_dark_src": _resolve(cfg.get("logo_dark_url") or "") or logo,
            "favicon_src": _resolve(cfg["favicon_url"]),
        }
    }
