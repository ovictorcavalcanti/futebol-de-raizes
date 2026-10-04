"""Classificação gravada como cache: pode ser apagada e reconstruída a qualquer
momento a partir das partidas (ver standings/domain.py e standings/services.py)."""

from django.db import models

from competitions.models import Group, Team


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
    points = models.SmallIntegerField("Pts", default=0)
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
