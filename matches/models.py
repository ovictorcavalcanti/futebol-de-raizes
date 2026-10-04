"""Partidas, eventos (imutáveis), confrontos de mata-mata e enriquecimento.

Placar, status, período e confronto são caches gravados na mesma transação do
lançamento; a fonte da verdade é a lista de eventos (ver matches/domain.py).

As restrições do plano que sustentam as regras valem no banco, não só no `clean()`
(que só o admin chama): CHECKs e `uniq_match_tie_leg` aqui; gatilhos da migração
`0002_structure_triggers` para o que envolve outras tabelas (grupo, rodada e
confronto da mesma fase da partida; mata-mata ⇔ confronto; fase com tabela ⇔ grupo;
`leg` ≤ `ties.legs`). Violação → `IntegrityError` (SQLSTATE check_violation).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from competitions.models import Group, Round, Stage, Team


class Tie(models.Model):
    """Confronto de mata-mata, de um jogo (legs=1) ou ida e volta (legs=2)."""

    class DecidedBy(models.TextChoices):
        AGGREGATE = "aggregate", "No agregado"
        EXTRA_TIME = "extra_time", "Na prorrogação"
        PENALTIES = "penalties", "Nos pênaltis"

    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="ties", verbose_name="fase")
    round = models.ForeignKey(Round, on_delete=models.PROTECT, related_name="ties", verbose_name="rodada")
    position = models.PositiveSmallIntegerField("ordem", default=1)
    legs = models.PositiveSmallIntegerField("jogos", choices=[(1, "Jogo único"), (2, "Ida e volta")])
    # Obrigatório no cadastro: sem default, o admin exige a escolha.
    extra_time = models.BooleanField("tem prorrogação", choices=[(True, "Sim"), (False, "Não")])
    team_a = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+", verbose_name="time A")
    team_b = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+", verbose_name="time B")
    winner_team = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="+", verbose_name="vencedor")
    decided_by = models.CharField("decidido", max_length=12, choices=DecidedBy.choices, blank=True)

    class Meta:
        db_table = "ties"
        ordering = ["stage", "round__number", "position"]
        constraints = [
            models.CheckConstraint(condition=models.Q(legs__in=[1, 2]), name="tie_legs_1_or_2"),
            models.CheckConstraint(condition=~models.Q(team_a=models.F("team_b")), name="tie_distinct_teams"),
        ]
        verbose_name = "confronto"
        verbose_name_plural = "confrontos"

    def __str__(self):
        return f"{self.team_a} × {self.team_b}"

    def clean(self):
        if self.round_id and self.stage_id and self.round.stage_id != self.stage_id:
            raise ValidationError({"round": "A rodada precisa ser da mesma fase do confronto."})
        if self.stage_id and self.stage.format != Stage.Format.KNOCKOUT:
            raise ValidationError({"stage": "Confronto só existe em fase de mata-mata."})
        if self.team_a_id and self.team_a_id == self.team_b_id:
            raise ValidationError("Os dois times do confronto precisam ser diferentes.")


class Match(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Agendado"
        DELAYED = "delayed", "Atrasado"
        LIVE = "live", "Ao vivo"
        FINISHED = "finished", "Encerrado"
        POSTPONED = "postponed", "Adiado"
        SUSPENDED = "suspended", "Suspenso"
        CANCELLED = "cancelled", "Cancelado"

    class Period(models.TextChoices):
        FIRST_HALF = "first_half", "1º tempo"
        HALF_TIME = "half_time", "Intervalo"
        SECOND_HALF = "second_half", "2º tempo"
        EXTRA_TIME = "extra_time", "Prorrogação"
        PENALTIES = "penalties", "Pênaltis"

    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="matches", verbose_name="fase")
    group = models.ForeignKey(Group, on_delete=models.PROTECT, null=True, blank=True, related_name="matches", verbose_name="grupo")
    round = models.ForeignKey(Round, on_delete=models.PROTECT, null=True, blank=True, related_name="matches", verbose_name="rodada")
    tie = models.ForeignKey(Tie, on_delete=models.PROTECT, null=True, blank=True, related_name="matches", verbose_name="confronto")
    leg = models.PositiveSmallIntegerField("jogo do confronto", null=True, blank=True, choices=[(1, "Ida / único"), (2, "Volta")])
    home_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="home_matches", verbose_name="mandante")
    away_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="away_matches", verbose_name="visitante")
    kickoff_at = models.DateTimeField("início (UTC)", db_index=True)
    finished_at = models.DateTimeField("fim (UTC)", null=True, blank=True, editable=False)
    venue = models.CharField("estádio", max_length=120, blank=True)
    city = models.CharField("cidade", max_length=80, blank=True)
    # Caches derivados dos eventos; não editáveis à mão.
    status = models.CharField("status", max_length=12, choices=Status.choices, default=Status.SCHEDULED, editable=False)
    period = models.CharField("período", max_length=12, choices=Period.choices, null=True, blank=True, editable=False)
    period_started_at = models.DateTimeField("início do período", null=True, blank=True, editable=False)
    home_score = models.PositiveSmallIntegerField("gols mandante", default=0, editable=False)
    away_score = models.PositiveSmallIntegerField("gols visitante", default=0, editable=False)
    home_penalties = models.PositiveSmallIntegerField("pênaltis mandante", null=True, blank=True, editable=False)
    away_penalties = models.PositiveSmallIntegerField("pênaltis visitante", null=True, blank=True, editable=False)
    version = models.PositiveIntegerField("versão", default=0, editable=False)
    # Enriquecimento (fase 10): público e renda (em centavos).
    attendance = models.PositiveIntegerField("público pagante", null=True, blank=True)
    revenue_cents = models.PositiveBigIntegerField("renda (centavos)", null=True, blank=True)

    class Meta:
        db_table = "matches"
        ordering = ["kickoff_at", "id"]
        indexes = [
            models.Index(fields=["stage", "round"], name="match_stage_round"),
            models.Index(fields=["status"], name="match_status"),
            models.Index(fields=["finished_at"], name="match_finished_at"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(tie__isnull=True) | models.Q(group__isnull=True), name="match_tie_xor_group"),
            models.CheckConstraint(condition=~models.Q(home_team=models.F("away_team")), name="match_distinct_teams"),
            models.CheckConstraint(
                condition=models.Q(period__isnull=True) | models.Q(status__in=["live", "suspended"]),
                name="match_period_only_live_or_suspended",
            ),
            models.CheckConstraint(
                condition=models.Q(tie__isnull=True, leg__isnull=True) | models.Q(tie__isnull=False, leg__in=[1, 2]),
                name="match_leg_with_tie",
            ),
            # Um jogo por (confronto, ida/volta). NULL é distinto no Postgres: partida de
            # grupo (sem confronto) não entra. O resto da estrutura (grupo/rodada/confronto da
            # mesma fase, mata-mata ⇔ confronto, leg ≤ ties.legs) é conferido por gatilhos
            # (migração 0002): envolve outras tabelas, fora do alcance de um CHECK.
            models.UniqueConstraint(
                fields=["tie", "leg"],
                name="uniq_match_tie_leg",
                violation_error_message="Já existe uma partida para este jogo do confronto.",
            ),
        ]
        permissions = [
            ("post_event", "Pode lançar eventos de partida"),
            ("void_event", "Pode cancelar lançamentos"),
            ("change_status", "Pode mudar o status da partida"),
        ]
        verbose_name = "partida"
        verbose_name_plural = "partidas"

    def __str__(self):
        return f"{self.home_team} × {self.away_team}"

    def clean(self):
        errors = {}
        if self.home_team_id and self.home_team_id == self.away_team_id:
            errors["away_team"] = "Mandante e visitante precisam ser diferentes."
        if self.group_id and self.stage_id and self.group.stage_id != self.stage_id:
            errors["group"] = "O grupo precisa ser da mesma fase da partida."
        if self.round_id and self.stage_id and self.round.stage_id != self.stage_id:
            errors["round"] = "A rodada precisa ser da mesma fase da partida."
        if self.tie_id and self.group_id:
            errors["tie"] = "Partida de mata-mata aponta para um confronto e não tem grupo."
        if self.stage_id:
            knockout = self.stage.format == Stage.Format.KNOCKOUT
            if knockout and not self.tie_id:
                errors["tie"] = "Em fase de mata-mata, a partida precisa de um confronto."
            if not knockout and self.tie_id:
                errors["tie"] = "Partida de grupo não tem confronto."
            if not knockout and not self.group_id:
                errors["group"] = "Em fase com tabela, a partida precisa de um grupo."
        if self.tie_id:
            tie = self.tie
            if tie.stage_id != self.stage_id:
                errors["tie"] = "O confronto precisa ser da mesma fase da partida."
            if not self.leg:
                errors["leg"] = "Informe se é o jogo de ida/único (1) ou de volta (2)."
            elif self.leg > tie.legs:
                errors["leg"] = "O confronto não aceita mais partidas do que o número de jogos."
            # Jogo repetido no confronto: `uniq_match_tie_leg` (validate_constraints no admin;
            # o banco recusa também a corrida de dois "Salvar" ao mesmo tempo).
            if {self.home_team_id, self.away_team_id} != {tie.team_a_id, tie.team_b_id}:
                errors["home_team"] = "Os times da partida precisam ser os do confronto."
        elif self.leg:
            errors["leg"] = "Só partidas de mata-mata têm jogo de ida/volta."
        if errors:
            raise ValidationError(errors)


class MatchEvent(models.Model):
    """Evento da partida: fato imutável. Nada é apagado nem editado.

    Erro do operador vira cancelamento (`voided_at`), que tira o evento das
    leituras e o deixa só na auditoria. Gol anulado é um fato do jogo: outro
    evento (`goal_annulled`) que aponta para o gol original em `annuls_event`.
    """

    class Source(models.TextChoices):
        OPERATOR = "operator", "Tela do operador"
        ADMIN = "admin", "Django Admin"
        SYSTEM = "system", "Sistema"
        FEED = "feed", "Feed de provedor"
        SCRIPT = "script", "Script"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="events", verbose_name="partida")
    sequence = models.PositiveIntegerField("sequência")
    type = models.CharField("tipo", max_length=24)
    period = models.CharField("período", max_length=12, null=True, blank=True)
    minute = models.PositiveSmallIntegerField("minuto", null=True, blank=True)
    stoppage = models.PositiveSmallIntegerField("acréscimo", null=True, blank=True)
    team = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="+", verbose_name="time")
    # Jogador sem cadastro: o nome vai no payload ("player"; substituição: "player_out"/"player_in").
    payload = models.JSONField("dados", default=dict, blank=True)
    annuls_event = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="annulments", verbose_name="anula o evento")
    voided_at = models.DateTimeField("cancelado em", null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+", verbose_name="cancelado por")
    idempotency_key = models.CharField("chave de idempotência", max_length=80)
    source = models.CharField("origem", max_length=12, choices=Source.choices, default=Source.OPERATOR)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", verbose_name="lançado por")
    created_at = models.DateTimeField("lançado em", db_index=True)

    class Meta:
        db_table = "match_events"
        ordering = ["match", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["match", "sequence"], name="uniq_event_sequence"),
            models.UniqueConstraint(fields=["match", "idempotency_key"], name="uniq_event_idempotency"),
            models.CheckConstraint(
                condition=models.Q(voided_at__isnull=True, voided_by__isnull=True) | models.Q(voided_at__isnull=False, voided_by__isnull=False),
                name="event_void_has_author",
            ),
        ]
        indexes = [models.Index(fields=["type", "created_at"], name="event_type_created")]
        verbose_name = "evento"
        verbose_name_plural = "eventos"

    def __str__(self):
        return f"#{self.sequence} {self.type}"


# --- Enriquecimento (fase 10) -------------------------------------------------


class MatchLineup(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="lineups", verbose_name="partida")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+", verbose_name="time")
    formation = models.CharField("esquema", max_length=12, blank=True, help_text="Ex.: 4-3-3")
    coach = models.CharField("técnico", max_length=80, blank=True)

    class Meta:
        db_table = "match_lineups"
        constraints = [models.UniqueConstraint(fields=["match", "team"], name="uniq_lineup_team")]
        verbose_name = "escalação"
        verbose_name_plural = "escalações"

    def __str__(self):
        return f"{self.team} em {self.match}"

    def clean(self):
        if self.match_id and self.team_id and self.team_id not in (self.match.home_team_id, self.match.away_team_id):
            raise ValidationError({"team": "O time precisa ser um dos dois da partida."})


class MatchLineupPlayer(models.Model):
    """Jogador escalado: só o nome (jogadores não têm cadastro), número e posição."""

    class Position(models.TextChoices):
        GOALKEEPER = "GK", "Goleiro"
        DEFENDER = "DF", "Defensor"
        MIDFIELDER = "MF", "Meio-campista"
        FORWARD = "FW", "Atacante"

    lineup = models.ForeignKey(MatchLineup, on_delete=models.CASCADE, related_name="entries", verbose_name="escalação")
    name = models.CharField("nome", max_length=80)
    number = models.PositiveSmallIntegerField("número", null=True, blank=True)
    position = models.CharField("posição", max_length=2, choices=Position.choices, blank=True)
    starter = models.BooleanField("titular", default=True)
    order = models.PositiveSmallIntegerField("ordem", default=0)

    class Meta:
        db_table = "match_lineup_players"
        ordering = ["lineup", "-starter", "order", "id"]
        verbose_name = "jogador escalado"
        verbose_name_plural = "jogadores escalados"

    def __str__(self):
        return self.name


class MatchOfficial(models.Model):
    class Role(models.TextChoices):
        REFEREE = "referee", "Árbitro"
        ASSISTANT = "assistant", "Assistente"
        FOURTH = "fourth", "Quarto árbitro"
        VAR = "var", "VAR"
        AVAR = "avar", "Assistente de VAR"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="officials", verbose_name="partida")
    role = models.CharField("função", max_length=12, choices=Role.choices)
    name = models.CharField("nome", max_length=80)
    state = models.CharField("UF", max_length=2, blank=True)
    order = models.PositiveSmallIntegerField("ordem", default=0)

    class Meta:
        db_table = "match_officials"
        ordering = ["match", "order", "id"]
        verbose_name = "arbitragem"
        verbose_name_plural = "arbitragem"

    def __str__(self):
        return f"{self.get_role_display()}: {self.name}"


class MatchBroadcast(models.Model):
    class Kind(models.TextChoices):
        OPEN_TV = "open_tv", "TV aberta"
        PAY_TV = "pay_tv", "TV fechada"
        STREAMING = "streaming", "Streaming"
        RADIO = "radio", "Rádio"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="broadcasts", verbose_name="partida")
    name = models.CharField("nome", max_length=60)
    url = models.URLField("link", blank=True)
    kind = models.CharField("tipo", max_length=12, choices=Kind.choices, default=Kind.STREAMING)
    order = models.PositiveSmallIntegerField("ordem", default=0)

    class Meta:
        db_table = "match_broadcasts"
        ordering = ["match", "order", "id"]
        verbose_name = "transmissão"
        verbose_name_plural = "transmissões"

    def __str__(self):
        return self.name


class MatchStat(models.Model):
    """Estatísticas digitadas (posse, finalizações...). Gols e cartões não entram:
    são derivados dos eventos e não se misturam com as digitadas."""

    class Key(models.TextChoices):
        POSSESSION = "possession", "Posse de bola (%)"
        SHOTS = "shots", "Finalizações"
        SHOTS_ON_TARGET = "shots_on_target", "Finalizações no gol"
        CORNERS = "corners", "Escanteios"
        FOULS = "fouls", "Faltas"
        OFFSIDES = "offsides", "Impedimentos"
        SAVES = "saves", "Defesas"
        PASSES = "passes", "Passes"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="stats", verbose_name="partida")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+", verbose_name="time")
    key = models.CharField("estatística", max_length=20, choices=Key.choices)
    value = models.PositiveIntegerField("valor")

    class Meta:
        db_table = "match_stats"
        ordering = ["match", "key", "team"]
        constraints = [models.UniqueConstraint(fields=["match", "team", "key"], name="uniq_match_stat")]
        verbose_name = "estatística"
        verbose_name_plural = "estatísticas"

    def __str__(self):
        return f"{self.get_key_display()}: {self.value}"

    def clean(self):
        if self.key == self.Key.POSSESSION and self.value is not None and self.value > 100:
            raise ValidationError({"value": "Posse de bola vai de 0 a 100."})
