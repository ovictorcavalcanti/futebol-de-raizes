"""Cadastro de times em lote (`manage.py import_teams`)."""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from competitions.models import Team

pytestmark = pytest.mark.django_db


def run(path, *args):
    from io import StringIO

    out = StringIO()
    call_command("import_teams", str(path), *args, stdout=out)
    return out.getvalue()


def test_csv_creates_teams_and_skips_existing(tmp_path):
    Team.objects.create(name="Sport", short_name="SPT")
    file = tmp_path / "times.csv"
    file.write_text(
        "nome;sigla;cidade;cor_principal;cor_secundaria;escudo_url\n"
        "Sport;spt;Recife;#D71920;#000000;\n"
        "Náutico;nau;Recife;#C8102E;#FFFFFF;https://exemplo.com/nautico.png\n"
        "\n"
        "Retrô;ret;Camaragibe;;;\n",
        encoding="utf-8",
    )
    out = run(file)
    assert "2 criado(s), 0 atualizado(s), 1 já existia(m)" in out
    nautico = Team.objects.get(name="Náutico")
    assert (nautico.short_name, nautico.city, nautico.crest_url) == ("NAU", "Recife", "https://exemplo.com/nautico.png")
    retro = Team.objects.get(name="Retrô")
    assert (retro.color_primary, retro.color_secondary) == ("#12306B", "#FFFFFF")  # padrões
    assert Team.objects.get(name="Sport").city == ""  # existente não muda sem --update


def test_json_update_and_dry_run(tmp_path):
    Team.objects.create(name="Santa Cruz", short_name="SCZ")
    file = tmp_path / "times.json"
    file.write_text(json.dumps([{"name": "santa cruz", "short_name": "sta", "city": "Recife"}]), encoding="utf-8")
    assert "Simulação (nada gravado): 0 criado(s), 1 atualizado(s)" in run(file, "--update", "--dry-run")
    assert Team.objects.get(short_name="SCZ").city == ""
    run(file, "--update")
    team = Team.objects.get()
    assert (team.short_name, team.city) == ("STA", "Recife")


def test_any_invalid_row_saves_nothing(tmp_path):
    file = tmp_path / "times.csv"
    file.write_text(
        "nome,sigla,cor_principal\n"
        "Central,CEN,#000000\n"
        "Afogados,AFOGADOS,#000000\n"  # sigla longa
        "Salgueiro,SAL,vermelho\n"  # cor inválida
        "Central,CEN2,\n",  # repetido
        encoding="utf-8",
    )
    with pytest.raises(CommandError) as info:
        run(file)
    message = str(info.value)
    assert "Nada foi gravado" in message and "Linha 3 (Afogados): sigla" in message
    assert "Linha 4 (Salgueiro): cor principal" in message and "Linha 5: Central repetido (já na linha 2)" in message
    assert not Team.objects.exists()


def test_unknown_column_and_missing_file(tmp_path):
    file = tmp_path / "times.csv"
    file.write_text("nome,apelido\nSport,Leão\n", encoding="utf-8")
    with pytest.raises(CommandError, match="coluna desconhecida apelido"):
        run(file)
    with pytest.raises(CommandError, match="Arquivo não encontrado"):
        run(tmp_path / "nao-existe.csv")
