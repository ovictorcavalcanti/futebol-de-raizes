import hashlib
import secrets

from django.db import models


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKey(models.Model):
    """Chave da API pública (fase 12). Só o hash é guardado."""

    name = models.CharField("nome", max_length=80)
    prefix = models.CharField("prefixo", max_length=12, unique=True, editable=False)
    key_hash = models.CharField("hash", max_length=64, unique=True, editable=False)
    active = models.BooleanField("ativa", default=True)
    rate_limit_per_minute = models.PositiveIntegerField("limite por minuto", default=60)
    created_at = models.DateTimeField("criada em", auto_now_add=True)
    last_used_at = models.DateTimeField("último uso", null=True, blank=True, editable=False)

    class Meta:
        db_table = "api_keys"
        ordering = ["name"]
        verbose_name = "chave da API pública"
        verbose_name_plural = "chaves da API pública"

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def generate(cls, name: str, rate_limit_per_minute: int = 60) -> tuple["ApiKey", str]:
        """Cria a chave e devolve (objeto, chave em texto). O texto só aparece aqui."""
        prefix = secrets.token_hex(4)
        raw = f"fdr_{prefix}_{secrets.token_urlsafe(24)}"
        obj = cls.objects.create(name=name, prefix=prefix, key_hash=hash_key(raw), rate_limit_per_minute=rate_limit_per_minute)
        return obj, raw
