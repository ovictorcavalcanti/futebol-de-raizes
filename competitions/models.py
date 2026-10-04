"""Estrutura: competições, temporadas, fases, grupos, rodadas, times e jogadores.

Pontuação, critérios de desempate e zonas da legenda são dados da fase, editáveis
no Django Admin. O código só conhece o catálogo de critérios (standings/domain.py).
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import IntegerRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

HEX_COLOR = RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Use o formato #RRGGBB.")


class Competition(models.Model):
    name = models.CharField("nome", max_length=120)
    slug = models.SlugField("slug", max_length=80, unique=True)
    position = models.PositiveIntegerField("ordem", default=0, db_index=True)
    short_name = models.CharField("nome curto", max_length=40, blank=True)

    class Meta:
        db_table = "competitions"
        ordering = ["position", "id"]
        verbose_name = "competição"
        verbose_name_plural = "competições"

    def __str__(self):
        return self.name


class Season(models.Model):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="seasons", verbose_name="competição")
    year = models.PositiveSmallIntegerField("ano")

    class Meta:
        db_table = "seasons"
        ordering = ["competition__position", "-year"]
        constraints = [models.UniqueConstraint(fields=["competition", "year"], name="uniq_season_year")]
        verbose_name = "temporada"
        verbose_name_plural = "temporadas"

    def __str__(self):
        return f"{self.competition} {self.year}"


class Stage(models.Model):
    class Format(models.TextChoices):
        LEAGUE = "league", "Pontos corridos"
        GROUPS = "groups", "Grupos"
        KNOCKOUT = "knockout", "Mata-mata"

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="stages", verbose_name="temporada")
    name = models.CharField("nome", max_length=80)
    position = models.PositiveIntegerField("ordem", default=0)
    format = models.CharField("formato", max_length=12, choices=Format.choices)
    points_win = models.PositiveSmallIntegerField("pontos por vitória", default=3)
    points_draw = models.PositiveSmallIntegerField("pontos por empate", default=1)
    points_loss = models.PositiveSmallIntegerField("pontos por derrota", default=0)

    class Meta:
        db_table = "stages"
        ordering = ["season", "position", "id"]
        verbose_name = "fase"
        verbose_name_plural = "fases"

    def __str__(self):
        return f"{self.season} · {self.name}"

    @property
    def has_table(self) -> bool:
        return self.format != self.Format.KNOCKOUT

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Pontos corridos ganha um grupo único automático: a classificação tem
        # um só caminho de código (sempre por grupo).
        if self.format == self.Format.LEAGUE and not self.groups.exists():
            Group.objects.create(stage=self, name="Tabela")


class StageCriterion(models.Model):
    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="criteria", verbose_name="fase")
    position = models.PositiveSmallIntegerField("ordem")
    key = models.CharField("critério", max_length=32)

    class Meta:
        db_table = "stage_criteria"
        ordering = ["stage", "position"]
        constraints = [
            models.UniqueConstraint(fields=["stage", "position"], name="uniq_stage_criterion_position"),
            models.UniqueConstraint(fields=["stage", "key"], name="uniq_stage_criterion_key"),
        ]
        verbose_name = "critério de desempate"
        verbose_name_plural = "critérios de desempate"

    def __str__(self):
        return f"{self.position}. {self.key}"

    def clean(self):
        from standings.domain import CRITERIA

        if self.key not in CRITERIA:
            raise ValidationError({"key": f"Critério desconhecido: {self.key}"})


class IntRange(models.Func):
    """`int4range(de, até, '[]')`: faixa fechada de posições (restrição de exclusão das zonas)."""

    function = "int4range"
    output_field = IntegerRangeField()


class StandingZone(models.Model):
    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="zones", verbose_name="fase")
    name = models.CharField("nome", max_length=60)
    color = models.CharField("cor", max_length=7, validators=[HEX_COLOR])
    position_from = models.PositiveSmallIntegerField("da posição")
    position_to = models.PositiveSmallIntegerField("até a posição")

    class Meta:
        db_table = "standing_zones"
        ordering = ["stage", "position_from"]
        constraints = [
            models.CheckConstraint(condition=models.Q(position_from__lte=models.F("position_to")), name="zone_range_ok"),
            models.CheckConstraint(condition=models.Q(color__regex=r"^#[0-9A-Fa-f]{6}$"), name="zone_color_hex"),
            models.CheckConstraint(condition=models.Q(position_from__gte=1), name="zone_from_positive"),
            # Faixas da mesma fase não se sobrepõem (btree_gist, migração 0002). Conferida no
            # commit (DEFERRED): o inline de zonas grava várias de uma vez, e uma troca como
            # 1-4/5-8 → 1-3/4-8 se sobrepõe no meio do caminho. No admin, quem avisa é
            # `standings.domain.validate_rules` (no inline a fase fica fora do formulário).
            ExclusionConstraint(
                name="zone_no_overlap",
                expressions=[
                    ("stage", RangeOperators.EQUAL),
                    (IntRange("position_from", "position_to", models.Value("[]")), RangeOperators.OVERLAPS),
                ],
                deferrable=models.Deferrable.DEFERRED,
                violation_error_message="As faixas de posição da fase não podem se sobrepor.",
            ),
        ]
        verbose_name = "zona da legenda"
        verbose_name_plural = "zonas da legenda"

    def __str__(self):
        return f"{self.name} ({self.position_from}–{self.position_to})"

    def clean(self):
        if self.position_from and self.position_to and self.position_from > self.position_to:
            raise ValidationError("Faixa invertida: a posição inicial é maior que a final.")


class Group(models.Model):
    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="groups", verbose_name="fase")
    name = models.CharField("nome", max_length=40)

    class Meta:
        db_table = "groups"
        ordering = ["stage", "name"]
        constraints = [models.UniqueConstraint(fields=["stage", "name"], name="uniq_group_name")]
        verbose_name = "grupo"
        verbose_name_plural = "grupos"

    def __str__(self):
        return f"{self.stage} · {self.name}"


class Round(models.Model):
    """Rodada pertence à fase (vale para todos os grupos dela)."""

    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="rounds", verbose_name="fase")
    number = models.PositiveSmallIntegerField("número")
    name = models.CharField("nome", max_length=60, blank=True)

    class Meta:
        db_table = "rounds"
        ordering = ["stage", "number"]
        constraints = [models.UniqueConstraint(fields=["stage", "number"], name="uniq_round_number")]
        verbose_name = "rodada"
        verbose_name_plural = "rodadas"

    def __str__(self):
        return self.name or f"Rodada {self.number}"

    @property
    def label(self) -> str:
        return self.name or f"Rodada {self.number}"


class Team(models.Model):
    name = models.CharField("nome", max_length=80)
    short_name = models.CharField("sigla", max_length=4, help_text="Até 4 letras, ex.: SPT")
    city = models.CharField("cidade", max_length=80, blank=True)
    color_primary = models.CharField("cor principal", max_length=7, default="#12306B", validators=[HEX_COLOR])
    color_secondary = models.CharField("cor secundária", max_length=7, default="#FFFFFF", validators=[HEX_COLOR])
    crest_url = models.URLField("escudo (URL)", blank=True)

    class Meta:
        db_table = "teams"
        ordering = ["name"]
        verbose_name = "time"
        verbose_name_plural = "times"

    def __str__(self):
        return self.name


class GroupTeam(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="group_teams", verbose_name="grupo")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="group_entries", verbose_name="time")
    lot_order = models.PositiveSmallIntegerField("ordem do sorteio", null=True, blank=True)

    class Meta:
        db_table = "group_teams"
        ordering = ["group", "team__name"]
        constraints = [models.UniqueConstraint(fields=["group", "team"], name="uniq_group_team")]
        verbose_name = "time do grupo"
        verbose_name_plural = "times do grupo"

    def __str__(self):
        return f"{self.team} em {self.group}"


class Player(models.Model):
    """Jogador (fase 10). Até lá, o nome do jogador vai no `payload` do evento."""

    class Position(models.TextChoices):
        GOALKEEPER = "GK", "Goleiro"
        DEFENDER = "DF", "Defensor"
        MIDFIELDER = "MF", "Meio-campista"
        FORWARD = "FW", "Atacante"

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="players", verbose_name="time")
    name = models.CharField("nome", max_length=80)
    number = models.PositiveSmallIntegerField("número", null=True, blank=True)
    position = models.CharField("posição", max_length=2, choices=Position.choices, blank=True)
    active = models.BooleanField("ativo", default=True)

    class Meta:
        db_table = "players"
        ordering = ["team", "number", "name"]
        verbose_name = "jogador"
        verbose_name_plural = "jogadores"

    def __str__(self):
        return f"{self.name} ({self.team.short_name})" if self.team_id else self.name
