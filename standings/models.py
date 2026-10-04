"""Classificação gravada como cache: pode ser apagada e reconstruída a qualquer
momento a partir das partidas e das punições (ver standings/domain.py e
standings/services.py). `PointAdjustment` (punição ou bonificação em pontos) é dado
de verdade, cadastrado na página da fase no Django Admin."""

from django.core.exceptions import ValidationError
from django.db import models

from competitions.models import HEX_COLOR, Group, GroupTeam, Season, Stage, Team


class PointAdjustment(models.Model):
    """Pontos tirados (negativo) ou dados (positivo) a um time numa fase.

    Entram na soma de pontos das duas tabelas (oficial e ao vivo), e o critério
    "Pontos" e a ordem usam os pontos ajustados; o confronto direto continua só com os
    resultados dos jogos. Vários ajustes do mesmo time na fase se somam."""

    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="point_adjustments", verbose_name="fase")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+", verbose_name="time")
    points = models.SmallIntegerField("pontos", help_text="Negativo tira pontos (punição); positivo dá pontos. Não pode ser 0.")
    reason = models.CharField("motivo", max_length=200, help_text="Aparece na legenda da classificação, ex.: escalação irregular.")
    created_at = models.DateTimeField("cadastrada em", auto_now_add=True)

    class Meta:
        db_table = "point_adjustments"
        ordering = ["stage", "created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(points=0), name="point_adjustment_not_zero"),
        ]
        verbose_name = "punição ou bonificação"
        verbose_name_plural = "punições e bonificações (pontos)"

    def __str__(self):
        return f"{self.team}: {self.points:+d} ({self.reason})" if self.team_id else self.reason

    def clean(self):
        errors = {}
        if self.points == 0:
            errors["points"] = "Informe um número diferente de zero (negativo tira pontos)."
        if self.stage_id and self.team_id:
            if not self.stage.has_table:
                errors["stage"] = "Fase de mata-mata não tem classificação."
            elif not GroupTeam.objects.filter(group__stage_id=self.stage_id, team_id=self.team_id).exists():
                errors["team"] = "O time precisa estar num dos grupos desta fase."
        if errors:
            raise ValidationError(errors)


class Standing(models.Model):
    class Kind(models.TextChoices):
        OFFICIAL = "official", "Oficial (só encerrados)"
        LIVE = "live", "Ao vivo"

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="standings", verbose_name="grupo")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="+", verbose_name="time")
    kind = models.CharField("tabela", max_length=8, choices=Kind.choices)
    position = models.PositiveSmallIntegerField("posição")
    played = models.PositiveSmallIntegerField("J", default=0)
    won = models.PositiveSmallIntegerField("V", default=0)
    drawn = models.PositiveSmallIntegerField("E", default=0)
    lost = models.PositiveSmallIntegerField("D", default=0)
    goals_for = models.PositiveSmallIntegerField("GP", default=0)
    goals_against = models.PositiveSmallIntegerField("GC", default=0)
    points = models.SmallIntegerField("Pts", default=0)  # já com a punição/bonificação (`adjustment`)
    adjustment = models.SmallIntegerField("ajuste de pontos", default=0)
    yellow_cards = models.PositiveSmallIntegerField("CA", default=0)
    red_cards = models.PositiveSmallIntegerField("CV", default=0)
    tied = models.BooleanField("empate não desfeito", default=False)

    class Meta:
        db_table = "standings"
        ordering = ["group", "kind", "position"]
        constraints = [
            models.UniqueConstraint(fields=["group", "kind", "team"], name="uniq_standing_team"),
        ]
        verbose_name = "linha da classificação"
        verbose_name_plural = "classificação"

    def __str__(self):
        return f"{self.position}. {self.team} ({self.points})"

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against


class Ranking(models.Model):
    """Classificação da temporada além das tabelas das fases: a geral do torneio (soma
    das fases marcadas, todos os jogos, mata-mata inclusive) ou uma personalizada (só os
    times escolhidos, ex.: briga por vaga na Série D entre 7 dos 10). Calculada na hora a
    partir dos jogos, com pontuação, critérios e zonas próprios (sem cache)."""

    class Scope(models.TextChoices):
        OVERALL = "overall", "Geral do torneio"
        CUSTOM = "custom", "Personalizada (times escolhidos)"

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="rankings", verbose_name="temporada")
    name = models.CharField("nome", max_length=80, help_text="Ex.: Classificação geral, Vaga na Série D.")
    scope = models.CharField("tipo", max_length=8, choices=Scope.choices, default=Scope.OVERALL)
    position = models.PositiveSmallIntegerField("ordem", default=0)
    stages = models.ManyToManyField(
        Stage, related_name="rankings_included", verbose_name="fases que entram",
        help_text="Os jogos destas fases somam (todos, mata-mata inclusive). Deixe de fora as que não contam.",
    )
    teams = models.ManyToManyField(
        Team, blank=True, related_name="rankings", verbose_name="times",
        help_text="Só na personalizada: os times que disputam (os demais não aparecem).",
    )
    points_win = models.PositiveSmallIntegerField("pontos por vitória", default=3)
    points_draw = models.PositiveSmallIntegerField("pontos por empate", default=1)
    points_loss = models.PositiveSmallIntegerField("pontos por derrota", default=0)
    show_on_competition = models.BooleanField(
        "mostrar na página da competição", default=False, help_text="Aparece como botão ao lado da classificação."
    )
    show_on_stages = models.ManyToManyField(
        Stage, blank=True, related_name="rankings_shown", verbose_name="mostrar na página destas fases",
        help_text="Botão ao lado da classificação quando a página mostra uma destas fases.",
    )

    class Meta:
        db_table = "rankings"
        ordering = ["season", "position", "id"]
        verbose_name = "classificação geral ou personalizada"
        verbose_name_plural = "classificações gerais e personalizadas"

    def __str__(self):
        return f"{self.name} ({self.season})"


class RankingCriterion(models.Model):
    ranking = models.ForeignKey(Ranking, on_delete=models.CASCADE, related_name="criteria", verbose_name="classificação")
    position = models.PositiveSmallIntegerField("ordem")
    key = models.CharField("critério", max_length=32)

    class Meta:
        db_table = "ranking_criteria"
        ordering = ["ranking", "position"]
        constraints = [models.UniqueConstraint(fields=["ranking", "position"], name="uniq_ranking_criterion_position")]
        verbose_name = "critério de desempate"
        verbose_name_plural = "critérios de desempate (na ordem)"


class RankingZone(models.Model):
    ranking = models.ForeignKey(Ranking, on_delete=models.CASCADE, related_name="zones", verbose_name="classificação")
    name = models.CharField("nome", max_length=60)
    color = models.CharField("cor", max_length=7, validators=[HEX_COLOR])
    position_from = models.PositiveSmallIntegerField("da posição")
    position_to = models.PositiveSmallIntegerField("até a posição")

    class Meta:
        db_table = "ranking_zones"
        ordering = ["ranking", "position_from"]
        verbose_name = "zona"
        verbose_name_plural = "zonas (faixas coloridas)"
