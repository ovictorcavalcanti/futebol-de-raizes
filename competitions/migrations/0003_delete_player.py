"""Jogadores sem cadastro: apaga `competitions.Player` (os nomes já foram copiados
para os lances e as escalações em `matches.0003_remove_players`)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("competitions", "0002_zone_no_overlap"),
        ("matches", "0003_remove_players"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Player",
        ),
    ]
