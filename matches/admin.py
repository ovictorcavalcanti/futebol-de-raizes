"""Django Admin das rodadas, partidas, confrontos e escalações.

Toda escrita passa pelo mesmo núcleo dos lançamentos (matches/services.py):

* Rodada: a página da rodada lista os jogos dela (mandante, visitante, início, local;
  grupo só entre os da fase; no mata-mata, confronto e jogo de ida/volta só entre os
  confrontos da rodada) e, no mata-mata, os confrontos. Cada jogo incluído ou alterado
  passa pela mesma conferência do formulário da partida (`MatchForm`, `Match.clean`,
  restrições do banco) e por `services.on_match_edited`; cada confronto alterado
  reapura e publica os jogos dele. Status e placar ficam somente leitura.
* Partida: só os campos de estrutura são editáveis (fase, grupo, rodada, confronto,
  times, horário, local, público e renda). Status, período e placar são calculados
  pelos lances. Mudança de fase, confronto ou times que deixaria inválidos os
  lançamentos já feitos (desta partida ou do outro jogo do confronto) é recusada no
  formulário (o domínio refaz os jogos com a configuração nova); partida de grupo é
  entre times do grupo. Depois de gravar, `services.on_match_edited` refaz o cache
  pelos eventos, recalcula classificação/confronto quando preciso e publica a partida.
  Partida com lançamentos não se apaga (eventos não são apagados).
* Lances: ficam dentro da partida, somente leitura (os cancelados somem). Lance errado
  se corrige marcando "Cancelar lançamento" na linha e salvando: chama
  `services.void_event` (exige `matches.void_event`); nenhuma linha de evento é gravada
  nem apagada pelo admin. Lançar lances novos é na tela do operador (link no topo da
  partida), que confere os campos de cada tipo.
* Confronto: com jogos cadastrados, fase e times não mudam e o número de jogos não
  fica menor que os jogos cadastrados; a mudança passa pela mesma conferência dos
  lançamentos e cada jogo é reapurado e publicado (`on_match_edited(jogo, ["tie"])`).
* Escalação (só nomes: jogadores não têm cadastro), arbitragem, transmissão e
  estatística: `services.publish_match`.
* Rodada, partida e confronto: o POST do formulário inteiro roda com a trava global de
  escrita (`WriteLockedPostMixin`): conferência e gravação sem corrida com outro "Salvar".
"""

from __future__ import annotations

import copy

from django import forms
from django.contrib import admin, messages
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from competitions.admin import (
    BaseAdmin,
    HiddenFromIndexMixin,
    HierarchyAdminMixin,
    Inline,
    PreloadedChoicesFormSet,
    WriteLockedPostMixin,
    admin_link,
    open_link,
)
from competitions.models import Group, GroupTeam, Round, Stage, Team
from core.locks import locked_atomic

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
    "partial_info",
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
    "tie__round",
    "tie__team_a",
    "tie__team_b",
    "home_team",
    "away_team",
)
OPS_PERMISSIONS = ("matches.post_event", "matches.change_status", "matches.void_event")
VOID_REASON = "Cancelado na página da partida (Django Admin)"


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
    return payload.get("player") or ""


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


def score_label(match: Match) -> str:
    if match.status == Match.Status.SCHEDULED:
        return "—"
    text = f"{match.home_score} × {match.away_score}"
    if match.home_penalties is not None:
        text += f" ({match.home_penalties} × {match.away_penalties} pên.)"
    return text


def no_related_links(field) -> None:
    """Lista de escolha sem os atalhos de incluir/alterar/apagar o objeto escolhido."""
    for flag in ("can_add_related", "can_change_related", "can_delete_related", "can_view_related"):
        setattr(field.widget, flag, False)


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
class TieAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, WriteLockedPostMixin, BaseAdmin):
    """Confronto de mata-mata. Vencedor e forma da decisão são apurados pelos lances."""

    form = TieForm
    list_display = ("__str__", "stage", "round", "position", "legs", "extra_time", "winner_team", "decided_by")
    list_filter = ("stage__season__competition", "stage", "legs", "extra_time")
    search_fields = ("team_a__name", "team_a__short_name", "team_b__name", "team_b__short_name", "round__name")
    list_select_related = ("stage__season__competition", "round", "team_a", "team_b", "winner_team")
    autocomplete_fields = ("stage", "round", "team_a", "team_b")
    readonly_fields = ("winner_team", "decided_by", "matches_links")
    parent_params = (("round", Round), ("stage", Stage))
    fieldsets = (
        (None, {"fields": ("stage", "round", "position", ("legs", "extra_time"), ("team_a", "team_b"))}),
        ("Resultado (apurado pelos lances)", {"fields": ("winner_team", "decided_by", "matches_links")}),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change or not form.changed_data:
            return
        republish_tie_legs(form.instance)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("stage__season__competition", "round", "team_a", "team_b")

    @admin.display(description="jogos")
    def matches_links(self, obj):
        return tie_matches_links(obj)


def republish_tie_legs(tie: Tie) -> None:
    """O confronto vai junto de cada jogo (TieOut): o núcleo reapura o resultado
    (número de jogos e prorrogação mudam a apuração) e publica cada jogo."""
    for leg in Match.objects.filter(tie_id=tie.pk).order_by("leg", "id"):
        services.on_match_edited(leg, ["tie"])


def tie_matches_links(tie: Tie | None):
    if tie is None or tie.pk is None:
        return "—"
    legs = tie.matches.select_related("home_team", "away_team").order_by("leg", "id")
    rows = [
        (
            admin_link(match, f"Jogo {match.leg}: {match.home_team} {score_label(match)} {match.away_team}"),
            match.get_status_display(),
        )
        for match in legs
    ]
    if not rows:
        return "Nenhum jogo cadastrado ainda (cadastre na lista de jogos abaixo)."
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
            no_related_links(field)
        return formset


class VoidEventForm(forms.ModelForm):
    """Linha de lance: nada se edita; só a caixa "Cancelar lançamento"."""

    void = forms.BooleanField(label="Cancelar lançamento", required=False)

    class Meta:
        model = MatchEvent
        fields = ()


class MatchEventFormSet(BaseInlineFormSet):
    """Lances da partida. `save()` nunca grava nem apaga uma linha de evento: o
    cancelamento marcado é feito por `services.void_event` (MatchAdmin.save_formset)."""

    def voided_forms(self) -> list[forms.Form]:
        return [form for form in self.initial_forms if getattr(form, "cleaned_data", None) and form.cleaned_data.get("void")]

    def save(self, commit=True):
        self.new_objects, self.changed_objects, self.deleted_objects = [], [], []
        return []


class MatchEventInline(admin.TabularInline):
    """Lances visíveis da partida (mais recentes primeiro), somente leitura. Corrigir um
    erro = marcar "Cancelar lançamento" e salvar (precisa de `matches.void_event`)."""

    model = MatchEvent
    form = VoidEventForm
    formset = MatchEventFormSet
    fk_name = "match"
    fields = ("minute_label", "period_label", "type_label", "team", "summary", "launched", "void")
    readonly_fields = ("minute_label", "period_label", "type_label", "team", "summary", "launched")
    ordering = ("-sequence",)
    extra = 0
    max_num = 0
    can_delete = False
    verbose_name = "lance"
    verbose_name_plural = "lances (os cancelados não aparecem; para corrigir um erro, marque “Cancelar lançamento” e salve)"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(voided_at__isnull=True).select_related("team", "created_by")

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if not request.user.has_perm("matches.void_event"):
            return tuple(name for name in fields if name != "void")
        return fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        # "Alterar" aqui é só marcar o cancelamento (que vai pelo serviço).
        return request.user.has_perm("matches.void_event")

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) or request.user.has_perm("matches.void_event")

    @admin.display(description="minuto")
    def minute_label(self, obj):
        return domain.format_minute(obj.minute, obj.stoppage) or "—"

    @admin.display(description="período")
    def period_label(self, obj):
        return domain.PERIOD_LABELS.get(obj.period, "—") if obj.period else "—"

    @admin.display(description="lance")
    def type_label(self, obj):
        return event_label(obj)

    @admin.display(description="jogador e detalhes")
    def summary(self, obj):
        return " · ".join(part for part in (event_who(obj), event_details(obj)) if part) or "—"

    @admin.display(description="lançado por / em")
    def launched(self, obj):
        when = timezone.localtime(obj.created_at).strftime("%d/%m %H:%M") if obj.created_at else "—"
        who = obj.created_by.get_username() if obj.created_by_id else "—"
        return f"{who} · {when} ({obj.get_source_display()})"


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
class MatchAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, WriteLockedPostMixin, BaseAdmin):
    form = MatchForm
    list_display = ("kickoff_at", "match_label", "score", "status", "competition", "stage", "round")
    list_display_links = ("kickoff_at", "match_label")
    list_filter = ("status", "stage__season__competition", "stage")
    search_fields = ("home_team__name", "home_team__short_name", "away_team__name", "away_team__short_name", "venue", "city")
    date_hierarchy = "kickoff_at"
    list_select_related = MATCH_RELATED
    autocomplete_fields = ("stage", "group", "round", "tie", "home_team", "away_team")
    readonly_fields = (*MATCH_CACHE_FIELDS, "revenue_display", "lineups_links")
    inlines = [MatchEventInline, MatchOfficialInline, MatchBroadcastInline, MatchStatInline]
    parent_params = (("round", Round), ("stage", Stage))
    fieldsets = (
        (None, {"fields": ("stage", ("group", "round"), ("tie", "leg"), ("home_team", "away_team"), "kickoff_at", ("venue", "city"), "partial_info")}),
        (
            "Situação e placar (calculados pelos lances)",
            {
                "fields": (("status", "period"), ("home_score", "away_score"), ("home_penalties", "away_penalties"), "finished_display", "version"),
                "description": "Ninguém edita o placar: ele é a contagem dos gols válidos. "
                "Lance errado se corrige cancelando o lançamento na lista de lances abaixo.",
            },
        ),
        ("Público e renda", {"fields": (("attendance", "revenue_cents"), "revenue_display")}),
        ("Escalações", {"fields": ("lineups_links",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(*MATCH_RELATED)

    def operator_url(self, obj: Match) -> str:
        day = timezone.localtime(obj.kickoff_at).date().isoformat()
        return f"{reverse('operator')}?date={day}&match={obj.pk}"

    def operator_tool(self, request, obj):
        if obj is None or obj.pk is None or not any(request.user.has_perm(perm) for perm in OPS_PERMISSIONS):
            return []
        return [{"label": "Lançar lances na tela do operador", "url": self.operator_url(obj)}]

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        # O atalho para o operador fica só no aviso do topo (não ao lado de "Histórico").
        tools = self.operator_tool(request, obj) if obj is not None else []
        if tools:
            context["fdr_callout"] = {
                "text": "Gols, cartões, substituições, VAR e o andamento do jogo são lançados na tela do operador, "
                "que confere os campos de cada tipo de lance. Aqui você corrige a estrutura do jogo e cancela um lance errado.",
                "label": tools[0]["label"],
                "url": tools[0]["url"],
            }
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def get_deleted_objects(self, objs, request):
        """Partida com lançamentos não se apaga: eventos nunca são apagados (nem em cascata)."""
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        ids = [obj.pk for obj in objs]
        if MatchEvent.objects.filter(match_id__in=ids).exists():
            perms_needed.add("eventos (partida com lançamentos não se apaga)")
        return to_delete, model_count, perms_needed, protected

    def save_model(self, request, obj, form, change):
        with locked_atomic():  # na fila dos lançamentos (que gravam a mesma linha)
            if change:
                # Só os campos editáveis: o cache (placar, status...) é do caminho de escrita.
                obj.save(update_fields=[name for name in MATCH_EDITABLE_FIELDS if name in form.fields])
            else:
                obj.save()

    def save_formset(self, request, form, formset, change):
        if formset.model is MatchEvent:
            self.void_events(request, formset)
            formset.save()  # não grava nada (MatchEventFormSet)
            return
        super().save_formset(request, form, formset, change)

    def void_events(self, request, formset: MatchEventFormSet) -> None:
        """Cancela cada lance marcado por `services.void_event`, do mais novo ao mais antigo
        (assim o início de jogo cai depois do que veio após ele). O que já caiu junto
        (vermelho automático, anulação) não é pedido de novo."""
        events = sorted((form.instance for form in formset.voided_forms()), key=lambda event: -event.sequence)
        if not events:
            return
        request._fdr_stay = True  # volta para a própria partida, com a lista de lances refeita
        if not request.user.has_perm("matches.void_event"):
            self.message_user(request, "Seu perfil não pode cancelar lançamentos.", messages.ERROR)
            return
        selected = {event.sequence for event in events}
        fallen: set[int] = set()
        voided = already = 0
        for event in events:
            origin = domain.derived_from(event)
            if event.id in fallen or (origin is not None and origin in selected):
                continue  # cai (ou já caiu) com o lançamento de origem
            label = f"{event_label(event)} ({domain.format_minute(event.minute, event.stoppage) or 'sem minuto'})"
            try:
                outcome = services.void_event(event.match_id, event.id, request.user, reason=VOID_REASON, request=request)
            except DomainError as exc:
                self.message_user(request, f"Não deu para cancelar {label}: {exc.message}", messages.ERROR)
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

    def _redirect_up(self, request, obj, default):
        if getattr(request, "_fdr_stay", False):
            return HttpResponseRedirect(reverse("admin:matches_match_change", args=[obj.pk]))
        return super()._redirect_up(request, obj, default)

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
        return score_label(obj)

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
                label = f"Editar escalação: {team.name} ({lineup.formation or 'sem esquema'}, {lineup.entries.count()} jogadores)"
                links.append((admin_link(lineup, label),))
            else:
                links.append((format_html('<a href="{}?match={}&amp;team={}">Cadastrar escalação: {}</a>', add_url, obj.pk, team.pk, team.name),))
        return format_html_join(format_html("<br>"), "{}", links)


# --- Escalação --------------------------------------------------------------------------------


class MatchLineupPlayerInline(Inline):
    """Jogadores escalados: só o nome (obrigatório), número e posição."""

    model = MatchLineupPlayer
    fields = ("order", "starter", "name", "number", "position")
    ordering = ("-starter", "order", "id")


LINEUP_JSON_HELP = (
    "Opcional. Cole a escalação em JSON para SUBSTITUIR a lista de jogadores abaixo. "
    "Formato: uma lista de jogadores, cada um com \"nome\" (obrigatório), \"posicao\" "
    "(GOL, LAD, ZAG, LAE, VOL, MEI ou ATA), \"numero\" e \"titular\" (true/false; padrão true). "
    "Também aceita um objeto com \"esquema\", \"tecnico\" e \"jogadores\". Exemplo: "
    '{"esquema": "4-3-3", "tecnico": "Fulano", "jogadores": ['
    '{"nome": "Ivan", "posicao": "GOL", "numero": 1, "titular": true}, '
    '{"nome": "Biel", "posicao": "VOL", "numero": 5, "titular": true}, '
    '{"nome": "Kayo", "posicao": "ATA", "numero": 19, "titular": false}]}'
)
# Sigla do JSON → posição gravada (aceita também o código interno e o nome por extenso).
LINEUP_JSON_POSITIONS = {"GOL": "GK", "LAD": "LAD", "ZAG": "DF", "LAE": "LAE", "VOL": "VOL", "MEI": "MF", "ATA": "FW"}


def parse_lineup_json(raw: str) -> dict:
    """{"formation", "coach", "players": [{name, position, number, starter}]} ou ValidationError."""
    import json

    from django.core.exceptions import ValidationError

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"JSON inválido (linha {exc.lineno}, coluna {exc.colno}).") from None
    formation = coach = None
    if isinstance(data, dict):
        formation, coach = data.get("esquema"), data.get("tecnico")
        data = data.get("jogadores")
    if not isinstance(data, list) or not data:
        raise ValidationError("Envie uma lista de jogadores (ou um objeto com \"jogadores\").")
    positions = {**LINEUP_JSON_POSITIONS, **{code: code for code in MatchLineupPlayer.Position.values}}
    positions.update({label.casefold(): code for code, label in MatchLineupPlayer.Position.choices})
    players = []
    for index, item in enumerate(data, start=1):
        where = f"Jogador {index}"
        if not isinstance(item, dict):
            raise ValidationError(f"{where}: use um objeto com nome, posicao, numero e titular.")
        name = item.get("nome")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"{where}: \"nome\" é obrigatório.")
        position = item.get("posicao") or ""
        if position:
            code = positions.get(str(position).strip().upper()) or positions.get(str(position).strip().casefold())
            if code is None:
                raise ValidationError(f"{where}: posição \"{position}\" desconhecida. Use GOL, LAD, ZAG, LAE, VOL, MEI ou ATA.")
            position = code
        number = item.get("numero")
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 99):
            raise ValidationError(f"{where}: \"numero\" deve ser um inteiro de 0 a 99.")
        starter = item.get("titular", True)
        if not isinstance(starter, bool):
            raise ValidationError(f"{where}: \"titular\" deve ser true ou false.")
        players.append({"name": name.strip()[:80], "position": position, "number": number, "starter": starter})
    return {"formation": formation, "coach": coach, "players": players}


class MatchLineupForm(forms.ModelForm):
    lineup_json = forms.CharField(
        label="Escalação em JSON", required=False, help_text=LINEUP_JSON_HELP,
        widget=forms.Textarea(attrs={"rows": 6, "style": "font-family: monospace; width: 100%"}),
    )

    class Meta:
        model = MatchLineup
        fields = ("match", "team", "formation", "coach")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        match = self.instance.match if self.instance.match_id else None
        match_id = self.data.get("match") or self.initial.get("match")
        if match is None and match_id:
            match = Match.objects.filter(pk=getattr(match_id, "pk", match_id)).first()
        # Só os dois times do jogo (sem jogo escolhido ainda: nenhum).
        ids = [match.home_team_id, match.away_team_id] if match else []
        self.fields["team"].queryset = self.fields["team"].queryset.filter(pk__in=ids)

    def clean_lineup_json(self):
        raw = (self.cleaned_data.get("lineup_json") or "").strip()
        return parse_lineup_json(raw) if raw else None


@admin.register(MatchLineup)
class MatchLineupAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, BaseAdmin):
    form = MatchLineupForm
    list_display = ("match", "team", "formation", "coach", "match_kickoff")
    list_filter = ("match__stage__season__competition",)
    search_fields = ("team__name", "coach", "match__home_team__name", "match__away_team__name")
    list_select_related = ("match__home_team", "match__away_team", "team")
    autocomplete_fields = ("match",)
    inlines = [MatchLineupPlayerInline]
    parent_params = (("match", Match),)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "match__home_team", "match__away_team", "match__round__stage__season__competition", "match__tie__round", "team"
        )

    @admin.display(description="início", ordering="match__kickoff_at")
    def match_kickoff(self, obj):
        return obj.match.kickoff_at

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        imported = form.cleaned_data.get("lineup_json")
        if imported:
            lineup = form.instance
            lineup.entries.all().delete()
            MatchLineupPlayer.objects.bulk_create(
                MatchLineupPlayer(lineup=lineup, order=order, **player) for order, player in enumerate(imported["players"], start=1)
            )
            updates = {key: value for key, value in (("formation", imported["formation"]), ("coach", imported["coach"])) if value}
            if updates:
                limits = {"formation": 12, "coach": 80}
                MatchLineup.objects.filter(pk=lineup.pk).update(**{k: str(v)[: limits[k]] for k, v in updates.items()})
            messages.info(request, f"Escalação importada do JSON: {len(imported['players'])} jogadores.")
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


# --- Rodada: jogos (e confrontos, no mata-mata) ---------------------------------------------------


class RoundForm(forms.ModelForm):
    class Meta:
        model = Round
        fields = ("stage", "number", "name")

    def clean_stage(self):
        stage = self.cleaned_data["stage"]
        round_ = self.instance
        if round_.pk and stage is not None and stage.pk != round_.stage_id and (round_.matches.exists() or round_.ties.exists()):
            raise forms.ValidationError("A rodada já tem partidas ou confrontos: ela não muda de fase.")
        return stage


class RoundTieForm(TieForm):
    class Meta(TieForm.Meta):
        fields = ("position", "legs", "extra_time", "team_a", "team_b")


class RoundTieFormSet(PreloadedChoicesFormSet):
    """Confrontos da rodada: a fase do confronto é a da rodada."""

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        form.instance.stage = self.instance.stage
        return form


class RoundTieInline(Inline):
    model = Tie
    form = RoundTieForm
    formset = RoundTieFormSet
    fields = ("position", "legs", "extra_time", "team_a", "team_b", "result", "matches")
    readonly_fields = ("result", "matches")
    autocomplete_fields = ("team_a", "team_b")
    ordering = ("position", "id")
    verbose_name_plural = "confrontos da rodada (cadastre os jogos de cada um na lista de jogos abaixo)"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("team_a", "team_b", "winner_team")

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        for name in ("team_a", "team_b"):
            no_related_links(formset.form.base_fields[name])
        return formset

    @admin.display(description="resultado")
    def result(self, obj):
        if obj is None or obj.pk is None or not obj.winner_team_id:
            return "—"
        return f"{obj.winner_team} ({obj.get_decided_by_display().lower()})"

    @admin.display(description="jogos")
    def matches(self, obj):
        return tie_matches_links(obj)


class RoundMatchFormSet(PreloadedChoicesFormSet):
    """Jogos da rodada. A fase do jogo é a da rodada; em pontos corridos o grupo é o
    único da fase. Jogo com lançamentos não se apaga."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stage = self.instance.stage if self.instance.stage_id else None
        self.single_group = stage.groups.first() if stage is not None and stage.format == Stage.Format.LEAGUE else None

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        match = form.instance
        match.stage = self.instance.stage
        if match.pk is None and self.single_group is not None:
            match.group = self.single_group
        return form

    def clean(self):
        super().clean()
        doomed = [form.instance.pk for form in self.deleted_forms if form.instance.pk]
        if doomed and MatchEvent.objects.filter(match_id__in=doomed).exists():
            raise forms.ValidationError(
                "Partida com lançamentos não se apaga (eventos nunca são apagados). "
                "Cancele os lançamentos na página da partida antes."
            )


class RoundMatchInline(Inline):
    """Jogos da rodada: estrutura editável; status e placar vêm dos lances."""

    model = Match
    form = MatchForm
    formset = RoundMatchFormSet
    readonly_fields = ("status_label", "score", "open")
    autocomplete_fields = ("home_team", "away_team")
    ordering = ("kickoff_at", "id")
    verbose_name = "jogo"
    verbose_name_plural = "jogos da rodada"

    def get_fields(self, request, obj=None):
        common = ("home_team", "away_team", "kickoff_at", "venue", "city", "status_label", "score", "open")
        if obj is None or obj.stage.format == Stage.Format.LEAGUE:
            return common
        if obj.stage.format == Stage.Format.KNOCKOUT:
            return ("tie", "leg", *common)
        return ("group", *common)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("home_team", "away_team", "group", "tie")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name in ("venue", "city") and field is not None:
            field.widget.attrs.update({"size": 14, "style": "width:11em"})
        return field

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        fields = formset.form.base_fields
        for name in ("home_team", "away_team"):
            no_related_links(fields[name])  # linha enxuta: só a busca do time
        if obj is not None and "group" in fields:
            fields["group"].queryset = Group.objects.filter(stage_id=obj.stage_id).order_by("name")
            fields["group"].choices = list(fields["group"].choices)
            no_related_links(fields["group"])
        if obj is not None and "tie" in fields:
            fields["tie"].queryset = Tie.objects.filter(round=obj).select_related("team_a", "team_b").order_by("position", "id")
            fields["tie"].choices = list(fields["tie"].choices)
            no_related_links(fields["tie"])
        return formset

    @admin.display(description="status")
    def status_label(self, obj):
        return obj.get_status_display() if obj is not None and obj.pk else "—"

    @admin.display(description="placar")
    def score(self, obj):
        return score_label(obj) if obj is not None and obj.pk else "—"

    @admin.display(description="página")
    def open(self, obj):
        return open_link(obj, "abrir (lances, escalações)")


@admin.register(Round)
class RoundAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, WriteLockedPostMixin, BaseAdmin):
    form = RoundForm
    list_display = ("__str__", "number", "stage")
    list_filter = ("stage__season__competition", "stage")
    search_fields = ("name", "number", "stage__name", "stage__season__competition__name")
    list_select_related = ("stage__season__competition",)
    autocomplete_fields = ("stage",)
    ordering = ("stage", "number")
    parent_params = (("stage", Stage),)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("stage__season__competition")

    def get_inlines(self, request, obj):
        if obj is None:
            return []
        if obj.stage.format == Stage.Format.KNOCKOUT:
            return [RoundTieInline, RoundMatchInline]
        return [RoundMatchInline]

    def save_formset(self, request, form, formset, change):
        if formset.model is Match:
            self.save_matches(request, formset)
            return
        super().save_formset(request, form, formset, change)
        if formset.model is Tie:
            for tie, _changed in formset.changed_objects:
                republish_tie_legs(tie)

    def save_matches(self, request, formset) -> None:
        """Cada jogo incluído ou alterado é gravado como na página da partida (só os campos
        de estrutura) e passa por `services.on_match_edited` (cache, classificação,
        confronto, publicação e auditoria `match.edit`)."""
        formset.save(commit=False)  # monta new/changed/deleted_objects (a mensagem do histórico)
        for match in formset.deleted_objects:
            match.delete()
        deleted = {form for form in formset.deleted_forms}
        for form in formset.forms:
            if form in deleted or not form.has_changed() or not getattr(form, "cleaned_data", None):
                continue
            match = form.instance
            if match.pk is None:
                match.save()
                previous = {name: None for name in (*form.changed_data, "stage", "group", "round")}
            else:
                match.save(update_fields=[name for name in MATCH_EDITABLE_FIELDS if name in form.changed_data])
                previous = {name: form.initial.get(name) for name in form.changed_data}
            services.on_match_edited(match, previous, user=request.user, request=request)
