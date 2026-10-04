"""Faixas de posição de uma mesma fase não se sobrepõem (docs/PLANO.md, "Restrições que
sustentam as regras"): restrição de exclusão GiST, conferida no commit (DEFERRED).
`btree_gist` dá o operador `=` do `stage_id` dentro do índice GiST."""

import django.contrib.postgres.constraints
import django.db.models.constraints
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models

import competitions.models


class Migration(migrations.Migration):

    dependencies = [
        ("competitions", "0001_initial"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.AddConstraint(
            model_name="standingzone",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                deferrable=django.db.models.constraints.Deferrable["DEFERRED"],
                expressions=[
                    ("stage", "="),
                    (
                        competitions.models.IntRange("position_from", "position_to", models.Value("[]")),
                        "&&",
                    ),
                ],
                name="zone_no_overlap",
                violation_error_message="As faixas de posição da fase não podem se sobrepor.",
            ),
        ),
    ]
