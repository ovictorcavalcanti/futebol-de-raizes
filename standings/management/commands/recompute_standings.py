"""Refaz o cache das classificações (tabela `standings`) de todas as fases com tabela.

    python manage.py recompute_standings

Use depois de uma atualização que muda o que fica gravado (ex.: a posição exibida `rank`). Sem
isso nada fica errado: a leitura calcula na hora, sem gravar, os grupos com cache velho, até o
próximo lance de cada um. Cada fase é refeita como numa mudança de regras no admin (trava de
escrita, publica a classificação nova para as páginas abertas).
"""

from django.core.management.base import BaseCommand

from competitions.models import Stage
from standings import services
from standings.domain import ConfigError


class Command(BaseCommand):
    help = "Refaz o cache das classificações de todas as fases com tabela."

    def handle(self, *args, **options):
        done = 0
        for stage in Stage.objects.order_by("id"):
            if not stage.has_table:
                continue
            try:
                services.on_stage_rules_changed(stage, recalc=True)
            except ConfigError as exc:
                self.stderr.write(f"Fase {stage.id} ({stage.name}): regras inválidas, ficou de fora — {exc}")
                continue
            done += 1
        self.stdout.write(f"{done} fase(s) recalculada(s).")
