"""Cria uma chave da API pública e mostra o texto dela (uma única vez).

Uso: `python manage.py create_api_key "Nome do parceiro" [--limit N]`
(padrão do limite: `settings.PUBLIC_API["DEFAULT_RATE_LIMIT_PER_MINUTE"]`).
A última linha da saída é só a chave, para scripts.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from observability import audit
from public_api.models import ApiKey

NAME_MAX_LENGTH = ApiKey._meta.get_field("name").max_length


class Command(BaseCommand):
    help = "Cria uma chave da API pública e mostra o texto dela (só desta vez)."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Nome de quem vai usar a chave (ex.: parceiro, app).")
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Requisições por minuto (padrão: PUBLIC_API_RATE_LIMIT, 60).",
        )

    def handle(self, *args, name, limit=None, **options):
        name = " ".join(name.split())
        if not name:
            raise CommandError("Informe o nome da chave.")
        if len(name) > NAME_MAX_LENGTH:
            raise CommandError(f"Nome com mais de {NAME_MAX_LENGTH} caracteres.")
        if limit is None:
            limit = settings.PUBLIC_API["DEFAULT_RATE_LIMIT_PER_MINUTE"]
        if limit < 1:
            raise CommandError("--limit deve ser um inteiro maior ou igual a 1.")
        with transaction.atomic():
            api_key, raw = ApiKey.generate(name, limit)
            audit.record(
                "api_key.create",
                obj=api_key,
                data={"name": api_key.name, "prefix": api_key.prefix, "rate_limit_per_minute": limit, "via": "create_api_key"},
            )
        self.stdout.write(
            self.style.SUCCESS(f"Chave criada: {api_key.name} (prefixo {api_key.prefix}, {limit} req/min).")
        )
        self.stdout.write("Guarde a chave agora: ela não será mostrada de novo.")
        self.stdout.write(raw)
