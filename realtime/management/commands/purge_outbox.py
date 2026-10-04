"""Apaga mensagens antigas do outbox (o hub já faz isso a cada hora).

Uso: `python manage.py purge_outbox [--hours N]` (padrão: retenção configurada, 24 h).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from realtime.outbox import purge_old


class Command(BaseCommand):
    help = "Apaga mensagens do outbox com mais de N horas (padrão: OUTBOX_RETENTION_HOURS, 24 h)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=None,
            help="Idade mínima, em horas, das mensagens apagadas (padrão: retenção configurada, 24 h).",
        )

    def handle(self, *args, hours=None, **options):
        if hours is not None and hours < 1:
            raise CommandError("--hours deve ser um inteiro maior ou igual a 1.")
        hours = hours or settings.REALTIME["OUTBOX_RETENTION_HOURS"]
        deleted = purge_old(hours)
        self.stdout.write(self.style.SUCCESS(f"Outbox: {deleted} mensagem(ns) com mais de {hours} h apagada(s)."))
