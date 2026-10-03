"""Django Admin das partidas, confrontos, escalações e eventos.

Toda escrita passa pelo mesmo núcleo dos lançamentos (matches/services.py):

* Partida: só os campos de estrutura são editáveis (fase, grupo, rodada, confronto,
  times, horário, local, público e renda). Status, período e placar são calculados
  pelos lances. Mudança de fase, confronto ou times que deixaria inválidos os
  lançamentos já feitos (desta partida ou do outro jogo do confronto) é recusada no
  formulário (o domínio refaz os jogos com a configuração nova); partida de grupo é
  entre times do grupo. Depois de gravar, `services.on_match_edited` refaz o cache
  pelos eventos, recalcula classificação/confronto quando preciso e publica a partida.
  Partida com lançamentos não se apaga (eventos não são apagados).
* Confronto: com jogos cadastrados, fase e times não mudam e o número de jogos não
  fica menor que os jogos cadastrados; a mudança passa pela mesma conferência dos
  lançamentos e cada jogo é reapurado e publicado (`on_match_edited(jogo, ["tie"])`).
* Escalação, arbitragem, transmissão e estatística: `services.publish_match`.
* Eventos: somente leitura. Lançamento errado se corrige com a ação "Cancelar
  lançamento", que chama `services.void_event` (exige `matches.void_event`).
"""

from __future__ import annotations

import copy

from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from competitions.admin import BaseAdmin, Inline, PreloadedChoicesFormSet, admin_link
from competitions.models import GroupTeam, Team
from core.locks import locked_atomic
from observability.admin import pretty_json

from . import context, domain, services
from .domain import CATALOG, DomainError
from .models import Match, MatchBroadcast, MatchEvent, MatchLineup, MatchLineupPlayer, MatchOfficial, MatchStat, Tie

# Campos que o admin edita; o resto da partida é cache dos eventos.
MATCH_EDITABLE_FIELDS = (
    "stage",
    "group",
    "round",
    "tie",
    "leg",
    "home_team",
    "away_team",
    "kickoff_at",
    "venue",
    "city",
    "attendance",
    "revenue_cents",
)
MATCH_CACHE_FIELDS = (
    "status",
    "period",
    "home_score",
    "away_score",
    "home_penalties",
    "away_penalties",
    "finished_display",
    "version",
)
MATCH_RELATED = (
    "stage__season__competition",
    "group",
    "round",
    "tie__team_a",
    "tie__team_b",
    "home_team",
    "away_team",
)


# Campos que mudam a forma como os lançamentos já gravados são lidos (times, confronto).
REPLAY_FIELDS = frozenset({"stage", "tie", "leg", "home_team", "away_team"})


def replay_problem(match: Match, legs: list[Match]) -> tuple[str, domain.MatchState | None]:
    """Refaz os lançamentos visíveis da partida com a configuração dada (times e
    confronto de `match`, outros jogos em `legs`). Devolve (mensagem do erro ou "",
    estado refeito ou None sem lançamentos)."""
    rows = context.match_events(match)
    if not any(row.voided_at is None for row in rows):
        return "", None
    try:
        state = domain.derive_state(context.to_domain_events(rows), context.build_context(match, legs=legs))
    except DomainError as exc:
        return exc.message, None
    return "", state


def sorted_legs(legs) -> list[Match]:
    return sorted(legs, key=lambda leg: (leg.leg or 0, leg.pk or 0))


def event_label(event: MatchEvent) -> str:
    spec = CATALOG.get(event.type)
    return spec.label if spec else event.type


def event_who(event: MatchEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.type == domain.EventType.SUBSTITUTION:
        out, entering = payload.get("player_out"), payload.get("player_in")
        return f"sai {out or '?'}, entra {entering or '?'}"
    if payload.get("player"):
        return payload["player"]
    if event.player_id:
        return event.player.name
    return ""


def event_details(event: MatchEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    parts = []
    if event.type == domain.EventType.GOAL:
        origin = payload.get("origin")
        if origin and origin != domain.GoalOrigin.OPEN_PLAY:
            parts.append(domain.GOAL_ORIGIN_LABELS.get(origin, origin))
        if payload.get("assist"):
            parts.append(f"assistência: {payload['assist']}")
    elif event.type == domain.EventType.RED_CARD and payload.get("reason") == domain.SECOND_YELLOW:
        parts.append("2º amarelo")
    elif event.type == domain.EventType.VAR_REVIEW:
        parts.append(f"{payload.get('incident', '')} → {payload.get('decision', '')}")
    elif event.type == domain.EventType.STOPPAGE_TIME:
        parts.append(f"+{payload.get('minutes')}")
    elif event.type == domain.EventType.SHOOTOUT_KICK:
        parts.append("convertida" if payload.get("scored") else "perdida")
    elif event.type == domain.EventType.RESCHEDULED:
        parts.append(f"para {payload.get('kickoff_at', '')}")
    if event.type == domain.EventType.GOAL_ANNULLED and event.annuls_event_id:
        parts.append(f"anula o lance #{event.annuls_event_id}")
    if payload.get("reason") and payload.get("reason") != domain.SECOND_YELLOW:
        parts.append(payload["reason"])
    return " · ".join(part for part in parts if part)


class EventDisplayMixin:
    """Colunas de leitura de um evento (inline da partida e lista de eventos)."""

    @admin.display(description="tipo")
    def type_label(self, obj):
        return event_label(obj)

    @admin.display(description="minuto")
    def minute_label(self, obj):
        return domain.format_minute(obj.minute, obj.stoppage) or "—"

    @admin.display(description="período")
    def period_label(self, obj):
        return domain.PERIOD_LABELS.get(obj.period, "—") if obj.period else "—"

    @admin.display(description="jogador")
    def who(self, obj):
        return event_who(obj) or "—"

    @admin.display(description="detalhes")
    def details(self, obj):
        return event_details(obj) or "—"

    @admin.display(description="situação")
    def voided_label(self, obj):
        if obj.voided_at is None:
            return "válido"
        when = timezone.localtime(obj.voided_at).strftime("%d/%m %H:%M")
        return format_html('<strong style="color:var(--error-fg)">cancelado</strong> por {} em {}', obj.voided_by, when)


# --- Confronto ------------------------------------------------------------------------------


class TieForm(forms.ModelForm):
    """Confronto com jogos cadastrados: a fase e os times não mudam (os jogos são
    desses times), o número de jogos não fica menor que os jogos cadastrados, e
    nenhuma mudança pode deixar inválidos os lançamentos já feitos (ex.: tirar a
    prorrogação de um jogo que já está nela)."""

    class Meta:
        model = Tie
        fields = ("stage", "round", "position", "legs", "extra_time", "team_a", "team_b")

    def _post_clean(self):
        tie = self.instance
        # Antes de super(): a instância ainda tem os valores gravados.
        stage_before, teams_before = tie.stage_id, {tie.team_a_id, tie.team_b_id}
        super()._post_clean()
        if self.errors or tie.pk is None:
            return
        legs = list(Match.objects.filter(tie_id=tie.pk).select_related("home_team", "away_team"))
        if not legs:
            return
        if tie.stage_id != stage_before:
            self.add_error("stage", "O confronto já tem jogos: ele não muda de fase.")
        if {tie.team_a_id, tie.team_b_id} != teams_before:
            self.add_error("team_a", "O confronto já tem jogos: os times não mudam (os jogos são desses dois times).")
        if tie.legs is not None and max(leg.leg or 1 for leg in legs) > tie.legs:
            self.add_error("legs", "O jogo de volta já está cadastrado: o confronto não pode virar jogo único.")
        if self.errors:
            return
        for leg in legs:
            leg.tie = tie  # o confronto como ficará (sem gravar)
        for leg in sorted_legs(legs):
            problem, _state = replay_problem(leg, sorted_legs(legs))
            if problem:
                self.add_error(None, f"Os lançamentos de {leg} ficariam inválidos com esta mudança: {problem}")


@admin.register(Tie)
class TieAdmin(BaseAdmin):
    """Confronto de mata-mata. Vencedor e forma da decisão são apurados pelos lances."""

    form = TieForm
    list_display = ("__str__", "stage", "round", "position", "legs", "extra_time", "winner_team", "decided_by")
    list_filter = ("stage__season__competition", "stage", "legs", "extra_time")
    search_fields = ("team_a__name", "team_a__short_name", "team_b__name", "team_b__short_name", "round__name")
    list_select_related = ("stage__season__competition", "round", "team_a", "team_b", "winner_team")
    autocomplete_fields = ("stage", "round", "team_a", "team_b")
    readonly_fields = ("winner_team", "decided_by", "matches_links")
    fieldsets = (
        (None, {"fields": ("stage", "round", "position", ("legs", "extra_time"), ("team_a", "team_b"))}),
        ("Resultado (apurado pelos lances)", {"fields": ("winner_team", "decided_by", "matches_links")}),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change or not form.changed_data:
            return
        # O confronto vai junto de cada jogo (TieOut): o núcleo reapura o resultado
        # (número de jogos e prorrogação mudam a apuração) e publica cada jogo.
        for leg in Match.objects.filter(tie_id=form.instance.pk).order_by("leg", "id"):
            services.on_match_edited(leg, ["tie"])

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("stage__season__competition", "round", "team_a", "team_b")

    @admin.display(description="jogos")
    def matches_links(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        legs = obj.matches.select_related("home_team", "away_team").order_by("leg", "id")
        rows = [
            (
                admin_link(match, f"Jogo {match.leg}: {match.home_team} {match.home_score} × {match.away_score} {match.away_team}"),
                match.get_status_display(),
            )
            for match in legs
        ]
        if not rows:
            return "Nenhum jogo cadastrado ainda."
        return format_html_join(format_html("<br>"), "{} ({})", rows)


# --- Partida --------------------------------------------------------------------------------


class MatchOfficialInline(Inline):
    model = MatchOfficial
    fields = ("order", "role", "name", "state")
    ordering = ("order", "id")


class MatchBroadcastInline(Inline):
    model = MatchBroadcast
    fields = ("order", "name", "kind", "url")
    ordering = ("order", "id")


class MatchStatInline(Inline):
    model = MatchStat
    fields = ("key", "team", "value")
    ordering = ("key", "team")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("team")

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj is not None and obj.pk:
            # Só os dois times da partida (a escolha fora deles é recusada), sem atalhos de cadastro.
            field = formset.form.base_fields["team"]
            field.queryset = Team.objects.filter(pk__in=[obj.home_team_id, obj.away_team_id])
            field.choices = list(field.choices)  # lida uma vez para todas as linhas
            for flag in ("can_add_related", "can_change_related", "can_delete_related", "can_view_related"):
                setattr(field.widget, flag, False)
        return formset


class MatchEventInline(EventDisplayMixin, admin.TabularInline):
    """Linha do tempo gravada: somente leitura (corrigir = cancelar lançamento)."""

    model = MatchEvent
    fk_name = "match"
    fields = ("sequence", "type_label", "period_label", "minute_label", "team", "who", "details", "source", "created_by", "created_at", "voided_label")
    readonly_fields = fields
    ordering = ("sequence",)
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    verbose_name_plural = "lançamentos (somente leitura — use “Cancelar lançamento” na lista de eventos)"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("team", "player", "created_by", "voided_by")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class MatchForm(forms.ModelForm):
    """Partida: além das regras do modelo (`Match.clean`), confere que os times são
    do grupo e que a mudança de fase, confronto ou times não deixa inválidos os
    lançamentos já feitos — desta partida e dos outros jogos do confronto (o domínio
    refaz cada jogo com a configuração nova; com erro, nada é gravado)."""

    class Meta:
        model = Match
        fields = MATCH_EDITABLE_FIELDS
        labels = {"kickoff_at": "início (horário de Brasília)"}
        help_texts = {"revenue_cents": "Em centavos (R$ 1.234,56 = 123456)."}

    def _post_clean(self):
        match = self.instance
        # Antes de super(): a instância ainda tem os valores gravados.
        tie_before, teams_before = match.tie_id, {match.home_team_id, match.away_team_id}
        super()._post_clean()
        if self.errors:
            return
        changed = set(self.changed_data)
        if match.pk is None or changed & {"stage", "group", "home_team", "away_team"}:
            self._check_group_teams(match)
        if match.pk is None or self.errors:
            return
        if changed & {"home_team", "away_team"}:
            self._check_leaving_teams(match, teams_before - {match.home_team_id, match.away_team_id})
        if not self.errors and changed & REPLAY_FIELDS:
            self._check_events(match, tie_before)

    def _check_group_teams(self, match: Match) -> None:
        """Partida de grupo é entre times do grupo (a classificação ignora os outros)."""
        if not match.group_id:
            return
        members = set(
            GroupTeam.objects.filter(group_id=match.group_id, team_id__in=[match.home_team_id, match.away_team_id]).values_list(
                "team_id", flat=True
            )
        )
        for name in ("home_team", "away_team"):
            team = getattr(match, name)
            if team.pk not in members:
                self.add_error(name, f"{team} não está no grupo “{match.group.name}”: cadastre o time no grupo antes.")

    def _check_leaving_teams(self, match: Match, leaving: set[int]) -> None:
        if not leaving:
            return
        lineups = MatchLineup.objects.filter(match=match, team_id__in=leaving).select_related("team")
        stats = MatchStat.objects.filter(match=match, team_id__in=leaving).select_related("team")
        names = sorted({str(item.team) for item in lineups} | {str(item.team) for item in stats})
        if names:
            self.add_error(
                None,
                f"Há escalação ou estatística de {', '.join(names)}, que sai da partida: apague-as antes de trocar o time.",
            )

    def _check_events(self, match: Match, tie_before: int | None) -> None:
        legs = sorted_legs([match, *Match.objects.filter(tie_id=match.tie_id).exclude(pk=match.pk)]) if match.tie_id else []
        problem, state = replay_problem(match, legs)
        if problem:
            self.add_error(
                None,
                f"Com essa mudança, os lançamentos já feitos nesta partida ficam inválidos: {problem} "
                "Cancele os lançamentos antes de mudar fase, confronto ou times.",
            )
            return
        # Os outros jogos dos confrontos (antigo e novo) são refeitos com o placar novo desta.
        probe = copy.copy(match)
        if state is not None:
            probe.status, probe.home_score, probe.away_score = state.status, state.home_score, state.away_score
        for tie_id in sorted({tie_before, match.tie_id} - {None}):
            others = list(Match.objects.filter(tie_id=tie_id).exclude(pk=match.pk).select_related("tie", "home_team", "away_team"))
            new_legs = sorted_legs(others + ([probe] if match.tie_id == tie_id else []))
            for leg in others:
                problem, _state = replay_problem(leg, new_legs)
                if problem:
                    self.add_error(None, f"Os lançamentos de {leg}, do mesmo confronto, ficariam inválidos com esta mudança: {problem}")


@admin.register(Match)
class MatchAdmin(BaseAdmin):
    form = MatchForm
    list_display = ("kickoff_at", "match_label", "score", "status", "competition", "stage", "round")
    list_display_links = ("kickoff_at", "match_label")
    list_filter = ("status", "stage__season__competition", "stage")
    search_fields = ("home_team__name", "home_team__short_name", "away_team__name", "away_team__short_name", "venue", "city")
    date_hierarchy = "kickoff_at"
    list_select_related = MATCH_RELATED
    autocomplete_fields = ("stage", "group", "round", "tie", "home_team", "away_team")
    readonly_fields = (*MATCH_CACHE_FIELDS, "revenue_display", "lineups_links")
    inlines = [MatchOfficialInline, MatchBroadcastInline, MatchStatInline, MatchEventInline]
    fieldsets = (
        (None, {"fields": ("stage", ("group", "round"), ("tie", "leg"), ("home_team", "away_team"), "kickoff_at", ("venue", "city"))}),
        (
            "Situação e placar (calculados pelos lances)",
            {
                "fields": (("status", "period"), ("home_score", "away_score"), ("home_penalties", "away_penalties"), "finished_display", "version"),
                "description": "Ninguém edita o placar: ele é a contagem dos gols válidos. "
                "Lance errado se corrige cancelando o lançamento.",
            },
        ),
        ("Público e renda", {"fields": (("attendance", "revenue_cents"), "revenue_display")}),
        ("Escalações", {"fields": ("lineups_links",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(*MATCH_RELATED)

    def save_model(self, request, obj, form, change):
        with locked_atomic():  # na fila dos lançamentos (que gravam a mesma linha)
            if change:
                # Só os campos editáveis: o cache (placar, status...) é do caminho de escrita.
                obj.save(update_fields=[name for name in MATCH_EDITABLE_FIELDS if name in form.fields])
            else:
                obj.save()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        match = form.instance
        if form.changed_data or not change:
            previous = {name: form.initial.get(name) for name in form.changed_data}
            services.on_match_edited(match, previous, user=request.user, request=request)
        elif any(formset.new_objects or formset.changed_objects or formset.deleted_objects for formset in formsets if hasattr(formset, "new_objects")):
            services.publish_match(match)

    @admin.display(description="partida", ordering="home_team__name")
    def match_label(self, obj):
        return f"{obj.home_team.short_name} × {obj.away_team.short_name}"

    @admin.display(description="placar")
    def score(self, obj):
        if obj.status == Match.Status.SCHEDULED:
            return "—"
        text = f"{obj.home_score} × {obj.away_score}"
        if obj.home_penalties is not None:
            text += f" ({obj.home_penalties} × {obj.away_penalties} pên.)"
        return text

    @admin.display(description="competição", ordering="stage__season__competition__position")
    def competition(self, obj):
        return obj.stage.season.competition

    @admin.display(description="fim (horário de Brasília)")
    def finished_display(self, obj):
        if obj is None or obj.finished_at is None:
            return "—"
        return timezone.localtime(obj.finished_at).strftime("%d/%m/%Y %H:%M:%S")

    @admin.display(description="renda")
    def revenue_display(self, obj):
        if obj is None or obj.revenue_cents is None:
            return "—"
        reais = f"{obj.revenue_cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {reais}"

    @admin.display(description="escalações")
    def lineups_links(self, obj):
        if obj is None or obj.pk is None:
            return "Salve a partida para cadastrar as escalações."
        lineups = {lineup.team_id: lineup for lineup in obj.lineups.select_related("team")}
        add_url = reverse("admin:matches_matchlineup_add")
        links = []
        for team in (obj.home_team, obj.away_team):
            lineup = lineups.get(team.pk)
            if lineup is not None:
                label = f"{team.name}: {lineup.formation or 'escalação'}"
                links.append((admin_link(lineup, label),))
            else:
                links.append((format_html('<a href="{}?match={}&amp;team={}">Cadastrar escalação: {}</a>', add_url, obj.pk, team.pk, team.name),))
        return format_html_join(format_html("<br>"), "{}", links)


# --- Escalação --------------------------------------------------------------------------------


class MatchLineupPlayerFormSet(PreloadedChoicesFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        lineup = self.instance
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or self._should_delete_form(form):
                continue
            player = data.get("player")
            if player is None and not data.get("name"):
                raise forms.ValidationError("Cada linha precisa de um jogador ou de um nome.")
            if player is not None and lineup.team_id and player.team_id != lineup.team_id:
                raise forms.ValidationError(f"{player.name} não é do time desta escalação.")


class MatchLineupPlayerInline(Inline):
    model = MatchLineupPlayer
    formset = MatchLineupPlayerFormSet
    fields = ("order", "starter", "player", "name", "number", "position")
    autocomplete_fields = ("player",)
    ordering = ("-starter", "order", "id")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("player__team")


@admin.register(MatchLineup)
class MatchLineupAdmin(BaseAdmin):
    list_display = ("match", "team", "formation", "coach", "match_kickoff")
    list_filter = ("match__stage__season__competition",)
    search_fields = ("team__name", "coach", "match__home_team__name", "match__away_team__name")
    list_select_related = ("match__home_team", "match__away_team", "team")
    autocomplete_fields = ("match", "team")
    inlines = [MatchLineupPlayerInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("match__home_team", "match__away_team", "team")

    @admin.display(description="início", ordering="match__kickoff_at")
    def match_kickoff(self, obj):
        return obj.match.kickoff_at

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        services.publish_match(form.instance.match)
        previous = form.initial.get("match")
        if change and "match" in form.changed_data and previous:
            services.publish_match(Match(pk=previous))

    def delete_model(self, request, obj):
        with locked_atomic():
            match = obj.match
            super().delete_model(request, obj)
            services.publish_match(match)

    def delete_queryset(self, request, queryset):
        with locked_atomic():
            match_ids = set(queryset.values_list("match_id", flat=True))
            super().delete_queryset(request, queryset)
            for match_id in sorted(match_ids):
                services.publish_match(Match(pk=match_id))


# --- Eventos ------------------------------------------------------------------------------------


class EventTypeFilter(admin.SimpleListFilter):
    title = "tipo"
    parameter_name = "tipo"

    def lookups(self, request, model_admin):
        return [(key, spec.label) for key, spec in CATALOG.items()]

    def queryset(self, request, queryset):
        return queryset.filter(type=self.value()) if self.value() else queryset


class VoidedFilter(admin.SimpleListFilter):
    title = "cancelado"
    parameter_name = "cancelado"

    def lookups(self, request, model_admin):
        return (("nao", "Válidos"), ("sim", "Cancelados"))

    def queryset(self, request, queryset):
        if self.value() == "sim":
            return queryset.filter(voided_at__isnull=False)
        if self.value() == "nao":
            return queryset.filter(voided_at__isnull=True)
        return queryset


@admin.register(MatchEvent)
class MatchEventAdmin(EventDisplayMixin, BaseAdmin):
    """Eventos: fatos imutáveis. Somente leitura, com a ação "Cancelar lançamento"."""

    list_display = ("created_at", "match", "sequence", "type_label", "minute_label", "team", "who", "source", "created_by", "voided_label")
    list_display_links = ("created_at", "match")
    list_filter = (EventTypeFilter, VoidedFilter, "source")
    search_fields = ("payload__player", "player__name", "match__home_team__name", "match__away_team__name")
    list_select_related = ("match__home_team", "match__away_team", "team", "player", "created_by", "voided_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")  # os lançamentos mais recentes primeiro
    actions = ["void_selected"]
    fields = (
        "match",
        "sequence",
        "type_label",
        "period_label",
        "minute_label",
        "team",
        "who",
        "details",
        "payload_view",
        "annuls_event",
        "source",
        "idempotency_key",
        "created_by",
        "created_at",
        "voided_label",
    )
    readonly_fields = fields
    show_full_result_count = False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "match__home_team", "match__away_team", "team", "player", "created_by", "voided_by", "annuls_event"
        )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_void_event_permission(self, request):
        return request.user.has_perm("matches.void_event")

    @admin.display(description="dados")
    def payload_view(self, obj):
        return pretty_json(obj.payload)

    @admin.action(description="Cancelar lançamento", permissions=["void_event"])
    def void_selected(self, request, queryset):
        """Cancela cada lançamento selecionado por `services.void_event`, do mais novo ao
        mais antigo de cada partida (assim o início de jogo cai depois do que veio após
        ele). O que já caiu junto nesta ação (derivado, anulação) não é pedido de novo."""
        voided = already = 0
        events = list(queryset.select_related("match__home_team", "match__away_team").order_by("match_id", "-sequence"))
        selected = {(event.match_id, event.sequence) for event in events}
        fallen: set[int] = set()  # ids cancelados nesta ação (o pedido e o que caiu junto)
        for event in events:
            origin = domain.derived_from(event)
            if event.id in fallen or (origin is not None and (event.match_id, origin) in selected):
                continue  # cai (ou já caiu) com o lançamento de origem
            try:
                outcome = services.void_event(event.match_id, event.id, request.user, reason="Cancelado pelo Django Admin", request=request)
            except DomainError as exc:
                self.message_user(request, f"{event.match} · #{event.sequence} {event_label(event)}: {exc.message}", messages.ERROR)
                continue
            if outcome.already:
                already += 1
            else:
                voided += 1
                fallen.update(outcome.voided_ids)
        if voided:
            self.message_user(request, f"{voided} lançamento(s) cancelado(s) (com o que caiu junto).", messages.SUCCESS)
        if already:
            self.message_user(request, f"{already} lançamento(s) já estava(m) cancelado(s).", messages.INFO)
