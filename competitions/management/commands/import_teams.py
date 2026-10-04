"""Cadastro de times em lote a partir de um JSON (ou, como alternativa, um CSV).

    python manage.py import_teams times.json [--update] [--dry-run]

JSON (formato preferido): uma lista de objetos, um por time —

    [
      {"nome": "Sport", "sigla": "SPT", "cidade": "Recife",
       "cor_principal": "#D71920", "cor_secundaria": "#000000",
       "escudo_url": "https://exemplo.com/sport.png"},
      {"nome": "Náutico", "sigla": "NAU"}
    ]

CSV (UTF-8, vírgula ou ponto e vírgula) com cabeçalho também é aceito. Chaves
(em português ou inglês):

    nome (name)                  obrigatório
    sigla (short_name)           obrigatório, até 4 letras (vira maiúscula)
    cidade (city)                opcional
    cor_principal (color_primary)     opcional, #RRGGBB (padrão #12306B)
    cor_secundaria (color_secondary)  opcional, #RRGGBB (padrão #FFFFFF)
    escudo_url (crest_url)       opcional, URL da imagem (arquivo: pelo admin)

O time é achado pelo nome (sem diferenciar maiúsculas): já existe → pulado, ou
atualizado com --update. Tudo ou nada: com qualquer linha inválida, nada é gravado.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from competitions.models import Team

# coluna do arquivo → campo do modelo
COLUMNS = {
    "nome": "name", "name": "name",
    "sigla": "short_name", "short_name": "short_name",
    "cidade": "city", "city": "city",
    "cor_principal": "color_primary", "color_primary": "color_primary",
    "cor_secundaria": "color_secondary", "color_secondary": "color_secondary",
    "escudo_url": "crest_url", "crest_url": "crest_url",
}
FIELD_LABELS = {field.name: field.verbose_name for field in Team._meta.fields}


def read_rows(path: Path) -> list[tuple[str, dict]]:
    """[(onde, {campo: valor})] do JSON ("Item 2") ou do CSV ("Linha 3"); chave desconhecida → erro."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CommandError(f"JSON inválido: {exc}") from exc
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise CommandError("O JSON deve ser uma lista de objetos, um por time.")
        raw = [(f"Item {n}", item) for n, item in enumerate(data, start=1)]
        what = "chave"
    else:
        try:
            dialect = csv.Sniffer().sniff(text.splitlines()[0] if text else ",", delimiters=",;")
        except csv.Error:  # uma coluna só: separador padrão
            dialect = csv.excel
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        raw = [(f"Linha {n}", row) for n, row in enumerate(reader, start=2)]  # linha 1 = cabeçalho
        what = "coluna"
    rows = []
    for number, item in raw:
        unknown = [key for key in item if key is not None and key.strip().lower() not in COLUMNS]
        if unknown:
            raise CommandError(f"{number}: {what} desconhecida {', '.join(unknown)}.")
        values = {COLUMNS[key.strip().lower()]: str(value or "").strip() for key, value in item.items() if key is not None}
        if any(values.values()):  # linha em branco: ignora
            rows.append((number, values))
    return rows


class Command(BaseCommand):
    help = "Cadastra times em lote a partir de um JSON (lista de objetos: nome, sigla, cidade, cores, escudo_url); CSV também vale."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Arquivo .json (preferido) ou .csv")
        parser.add_argument("--update", action="store_true", help="Atualiza os times que já existem (pelo nome).")
        parser.add_argument("--dry-run", action="store_true", help="Mostra o que seria feito, sem gravar.")

    def handle(self, *args, path, update=False, dry_run=False, **options):
        file = Path(path)
        if not file.is_file():
            raise CommandError(f"Arquivo não encontrado: {file}")
        rows = read_rows(file)
        if not rows:
            raise CommandError("Nenhum time no arquivo.")

        existing = {team.name.casefold(): team for team in Team.objects.all()}
        seen: dict[str, str] = {}
        errors, created, updated, skipped = [], [], [], []
        with transaction.atomic():
            for number, values in rows:
                name = values.get("name", "")
                if values.get("short_name"):
                    values["short_name"] = values["short_name"].upper()
                key = name.casefold()
                if key and key in seen:
                    errors.append(f"{number}: {name} repetido (já em {seen[key].lower()}).")
                    continue
                seen[key] = number
                team = existing.get(key)
                if team is not None and not update:
                    skipped.append(name)
                    continue
                target = team or Team()
                for field, value in values.items():
                    if value or field in ("name", "short_name"):
                        setattr(target, field, value)
                try:
                    target.full_clean(exclude=["crest_file"])
                except ValidationError as exc:
                    detail = "; ".join(
                        f"{FIELD_LABELS.get(field, field)}: {' '.join(messages)}" for field, messages in exc.message_dict.items()
                    )
                    errors.append(f"{number} ({name or 'sem nome'}): {detail}")
                    continue
                target.save()
                (updated if team else created).append(name)
            if errors or dry_run:
                transaction.set_rollback(True)

        if errors:
            raise CommandError("Nada foi gravado. Corrija o arquivo:\n" + "\n".join(errors))
        prefix = "Simulação (nada gravado): " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}{len(created)} criado(s), {len(updated)} atualizado(s), {len(skipped)} já existia(m)."
        ))
        if skipped and not update:
            self.stdout.write(f"Já existiam (use --update para atualizar): {', '.join(skipped)}.")
