"""Django Admin: o que cada perfil vê, regras da fase validadas e aplicadas pelo núcleo,
partida editada pelo caminho de escrita, lances só leitura dentro da partida com
"Cancelar lançamento", auditoria das ações do admin e a marca no cabeçalho.
(Navegação por competição, punições e o cancelamento pela página da partida: test_admin_navigation.py.)"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from competitions.models import Group, GroupTeam, Stage, StageCriterion, StandingZone, Team
from core import timeutils
from matches import services
from matches.domain import NewEvent
from matches.models import Match, MatchEvent, MatchLineup, MatchLineupPlayer, MatchOfficial, Tie
from observability.models import AuditLog
from realtime.models import Outbox
from standings.domain import Rules
from standings.models import Standing
from standings.services import recompute_group
from tests.factories import make_knockout, make_league, make_match, make_stage, make_tie_matches

pytestmark = pytest.mark.django_db


# --- Ajudantes ------------------------------------------------------------------------------


def url(obj_or_model, view="changelist", *args):
    meta = obj_or_model._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_{view}", args=args)


def change_url(obj):
    return url(obj, "change", obj.pk)


def _field_values(form, prefix=""):
    """Valores atuais de um formulário, como o navegador os enviaria."""
    data = {}
    for name, field in form.fields.items():
        key = f"{prefix}{name}"
        value = form[name].value()
        widget = field.widget
        subwidgets = getattr(widget, "widgets", None)
        if subwidgets and isinstance(value, datetime):
            local = timezone.localtime(value)
            data[f"{key}_0"] = local.strftime("%Y-%m-%d")
            data[f"{key}_1"] = local.strftime("%H:%M:%S")
        elif isinstance(value, bool):
            if value:
                data[key] = "on"
        elif value is None:
            data[key] = ""
        elif isinstance(value, (list, tuple)):
            data[key] = [str(item) for item in value]
        else:
            data[key] = str(value)
    return data


def post_data(response):
    """Corpo do POST da página de inclusão/alteração (formulário + todos os inlines)."""
    data = _field_values(response.context["adminform"].form)
    for inline in response.context["inline_admin_formsets"]:
        formset = inline.formset
        for name, value in formset.management_form.initial.items():
            data[f"{formset.prefix}-{name}"] = "" if value is None else str(value)
        for index, form in enumerate(formset.forms):
            data.update(_field_values(form, f"{formset.prefix}-{index}-"))
    return data


def set_formset_rows(data, prefix, rows, initial=0):
    """Troca as linhas de um inline: `rows` = lista de dicts (com "id" nas existentes)."""
    for key in [key for key in data if key.startswith(f"{prefix}-") and key.split("-")[1].isdigit()]:
        del data[key]
    for index, row in enumerate(rows):
        for name, value in row.items():
            data[f"{prefix}-{index}-{name}"] = "" if value is None else str(value)
    data[f"{prefix}-TOTAL_FORMS"] = str(len(rows))
    data[f"{prefix}-INITIAL_FORMS"] = str(initial)
    return data


def messages_text(response):
    return " | ".join(str(message) for message in response.context["messages"]) if response.context else ""


def last_outbox_id():
    row = Outbox.objects.order_by("-id").first()
    return row.id if row else 0


class Op:
    """Lança pelos serviços com relógio controlado."""

    def __init__(self, match, user):
        self.match = match
        self.user = user
        self.clock = timeutils.now() - timedelta(minutes=120)
        self.n = 0

    def post(self, type_, **fields):
        self.n += 1
        self.clock += timedelta(minutes=1)
        return services.post_event(
            self.match.id, self.user, NewEvent(type=type_, **fields), idempotency_key=f"t-{self.match.id}-{self.n}", at=self.clock
        )


@pytest.fixture
def league(db):
    return make_league(
        n_teams=4,
        team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"],
        zones=[{"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2}],
    )


def finish(match, user, home_goals=0, away_goals=0):
    op = Op(match, user)
    op.post("match_start")
    for n in range(home_goals):
        op.post("goal", minute=10 + n, team_id=match.home_team_id, payload={"player": f"Mandante {n}"})
    op.post("half_time")
    op.post("second_half_start")
    for n in range(away_goals):
        op.post("goal", minute=60 + n, team_id=match.away_team_id, payload={"player": f"Visitante {n}"})
    op.post("match_end")
    return op


@pytest.fixture
def full_data(league, operator_user):
    """Um pouco de tudo, para abrir as páginas de alteração de todos os modelos."""
    sport, nautico, santa, retro = league["teams"]
    match = make_match(league["stage"], sport, nautico, round=league["rounds"][0])
    finish(match, operator_user, 1, 1)
    lineup = MatchLineup.objects.create(match=match, team=sport, formation="4-3-3", coach="Técnico")
    MatchLineupPlayer.objects.create(lineup=lineup, name="Zé Roberto", number=10, position="MF")
    MatchOfficial.objects.create(match=match, role="referee", name="Árbitro Fulano", state="PE")
    knockout = make_knockout(league["season"], legs=1, extra_time=False, team_a=santa, team_b=retro)
    make_tie_matches(knockout["tie"])
    return {**league, "match": match}


# --- Páginas e perfis -------------------------------------------------------------------------


def test_admin_pages_load_for_administrator(admin_client_fdr, admin_user_fdr, full_data):
    """Lista, inclusão e alteração de todo modelo registrado abrem para o Administrador."""
    request_user = admin_user_fdr
    for model, model_admin in admin.site._registry.items():
        response = admin_client_fdr.get(url(model))
        assert response.status_code == 200, (model, response.status_code)
        add = admin_client_fdr.get(url(model, "add"))

        class FakeRequest:
            user = request_user

        expected = 200 if model_admin.has_add_permission(FakeRequest()) else 403
        assert add.status_code == expected, (model, add.status_code)
        obj = model._default_manager.order_by("pk").first()
        if obj is not None:
            change = admin_client_fdr.get(change_url(obj))
            assert change.status_code == 200, (model, change.status_code)


def test_operator_sees_operational_models_only(operator_client):
    for model in (Team, Stage, Match, MatchLineup):
        assert operator_client.get(url(model)).status_code == 200, model
    User = get_user_model()
    from django.contrib.auth.models import Group as AuthGroup

    for model in (User, AuthGroup, AuditLog, Outbox):
        assert operator_client.get(url(model)).status_code == 403, model
    from public_api.models import ApiKey

    if admin.site.is_registered(ApiKey):
        assert operator_client.get(url(ApiKey)).status_code == 403
    index = operator_client.get(reverse("admin:index")).content.decode()
    assert reverse("admin:competitions_competition_changelist") in index
    assert reverse("admin:accounts_user_changelist") not in index
    assert reverse("admin:observability_auditlog_changelist") not in index


def test_standing_has_no_page_and_outbox_and_audit_are_read_only(admin_client_fdr, full_data):
    assert Standing.objects.exists()
    assert not admin.site.is_registered(Standing)  # cache recalculado pelo sistema: sem página
    assert not admin.site.is_registered(MatchEvent)  # lances ficam dentro da partida
    assert admin_client_fdr.get(url(Outbox, "add")).status_code == 403
    assert admin_client_fdr.get(url(AuditLog, "add")).status_code == 403


def test_admin_is_branded(admin_client_fdr):
    page = admin_client_fdr.get(reverse("admin:index")).content.decode()
    assert "Futebol de Raízes · Administração" in page
    assert "img/logo.svg" in page
    assert "--fdr-frevo" in page and "#12306B" in page
    assert "GoalNow" not in page


# --- Regras da fase -----------------------------------------------------------------------------


def stage_form(client, stage):
    response = client.get(change_url(stage))
    assert response.status_code == 200
    return post_data(response)


def criteria_rows(stage, keys):
    existing = list(stage.criteria.order_by("position"))
    rows = []
    for index, key in enumerate(keys):
        row = {"position": index + 1, "key": key, "stage": stage.pk}
        row["id"] = existing[index].pk if index < len(existing) else ""
        rows.append(row)
    deleted = [{"id": item.pk, "position": item.position, "key": item.key, "stage": stage.pk, "DELETE": "on"} for item in existing[len(keys):]]
    return rows + deleted, min(len(existing), len(keys)) + len(deleted)


def test_stage_change_page_has_rules_inlines(admin_client_fdr, league):
    response = admin_client_fdr.get(change_url(league["stage"]))
    prefixes = {inline.formset.prefix for inline in response.context["inline_admin_formsets"]}
    assert {"criteria", "zones", "rounds"} <= prefixes
    page = response.content.decode()
    assert 'type="color"' in page
    assert "Confronto direto" in page  # rótulo do catálogo no select de critério


@pytest.mark.parametrize(
    "keys, message",
    [
        (["points", "inventado"], "desconhecido"),
        (["points", "wins", "points"], "Critério de desempate repetido: Pontos."),
    ],
)
def test_invalid_criteria_rejected_with_message(admin_client_fdr, league, keys, message):
    stage = league["stage"]
    data = stage_form(admin_client_fdr, stage)
    rows, initial = criteria_rows(stage, keys)
    set_formset_rows(data, "criteria", rows, initial=initial)
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200  # página reaberta com o erro
    assert message in response.content.decode()
    assert list(stage.criteria.order_by("position").values_list("key", flat=True)) == [
        "points", "wins", "goal_difference", "goals_for", "head_to_head"
    ]


def test_empty_criteria_rejected(admin_client_fdr, league):
    stage = league["stage"]
    data = stage_form(admin_client_fdr, stage)
    rows, initial = criteria_rows(stage, [])
    set_formset_rows(data, "criteria", rows, initial=initial)
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200
    assert "Informe ao menos um critério de desempate." in response.content.decode()
    assert stage.criteria.count() == 5


@pytest.mark.parametrize(
    "zones, message",
    [
        (
            [("Classificados", "#1B7F3B", 1, 2), ("Rebaixados", "#B3261E", 2, 4)],
            "se sobrepõem",
        ),
        ([("Classificados", "verde", 1, 2)], "#RRGGBB"),
        ([("Classificados", "#1B7F3B", 3, 1)], "Faixa invertida"),
    ],
)
def test_invalid_zones_rejected_with_message(admin_client_fdr, league, zones, message):
    stage = league["stage"]
    data = stage_form(admin_client_fdr, stage)
    existing = list(stage.zones.order_by("position_from"))
    rows = []
    for index, (name, color, start, end) in enumerate(zones):
        rows.append(
            {
                "id": existing[index].pk if index < len(existing) else "",
                "stage": stage.pk,
                "name": name,
                "color": color,
                "position_from": start,
                "position_to": end,
            }
        )
    set_formset_rows(data, "zones", rows, initial=len(existing))
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200
    assert message in response.content.decode()
    zone = stage.zones.get()
    assert (zone.color, zone.position_from, zone.position_to) == ("#1B7F3B", 1, 2)


def test_saving_criteria_recalculates_standings_and_publishes(admin_client_fdr, league, operator_user):
    sport, nautico, santa, retro = league["teams"]
    stage = league["stage"]
    finish(make_match(stage, sport, nautico), operator_user, 1, 0)  # Sport 3 pts, 1 gol
    finish(make_match(stage, santa, retro), operator_user, 4, 4)  # 1 pt e 4 gols para cada
    official = lambda: list(  # noqa: E731
        Standing.objects.filter(group=league["group"], kind="official").order_by("position").values_list("team__name", flat=True)
    )
    assert official()[0] == "Sport"
    mark = last_outbox_id()

    data = stage_form(admin_client_fdr, stage)
    rows, initial = criteria_rows(stage, ["goals_for", "points"])
    set_formset_rows(data, "criteria", rows, initial=initial)
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 302, messages_text(response) or response.content.decode()[:2000]

    assert list(stage.criteria.order_by("position").values_list("key", flat=True)) == ["goals_for", "points"]
    assert official()[0] in ("Santa Cruz", "Retrô")
    published = Outbox.objects.filter(id__gt=mark, topic="standings")
    assert published.exists()
    message = published.last().payload
    assert message["stage_id"] == stage.id
    assert [item["key"] for item in message["standings"]["criteria"]] == ["goals_for", "points"]
    assert AuditLog.objects.filter(action="admin.change", object_type="competitions.stage", object_id=str(stage.pk)).exists()


def test_swapping_two_criteria_does_not_break_unique_constraints(admin_client_fdr, league):
    stage = league["stage"]
    data = stage_form(admin_client_fdr, stage)
    rows, initial = criteria_rows(stage, ["wins", "points", "goal_difference", "goals_for", "head_to_head"])
    set_formset_rows(data, "criteria", rows, initial=initial)
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 302
    assert list(stage.criteria.order_by("position").values_list("key", flat=True))[:2] == ["wins", "points"]


def test_zone_change_publishes_standings_with_new_color(admin_client_fdr, league):
    stage = league["stage"]
    zone = stage.zones.get()
    mark = last_outbox_id()
    data = stage_form(admin_client_fdr, stage)
    data["zones-0-color"] = "#0e8a4a"  # o seletor nativo envia minúsculas
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 302
    zone.refresh_from_db()
    assert zone.color == "#0E8A4A"
    message = Outbox.objects.filter(id__gt=mark, topic="standings").last().payload
    assert message["standings"]["legend"][0]["color"] == "#0E8A4A"


def test_new_league_stage_comes_with_default_criteria(admin_client_fdr, league):
    response = admin_client_fdr.get(url(Stage, "add"))
    data = post_data(response)
    data.update({"season": league["season"].pk, "name": "2ª fase", "position": "2", "format": "league"})
    data.update({"points_win": "3", "points_draw": "1", "points_loss": "0"})
    response = admin_client_fdr.post(url(Stage, "add"), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    stage = Stage.objects.get(name="2ª fase")
    assert list(stage.criteria.order_by("position").values_list("key", flat=True)) == [
        "points", "wins", "goal_difference", "goals_for", "head_to_head"
    ]
    assert stage.groups.count() == 1  # grupo único automático


def test_group_teams_recompute_and_publish(admin_client_fdr, league):
    group = league["group"]
    newcomer = Team.objects.create(name="Central", short_name="CEN")
    mark = last_outbox_id()
    response = admin_client_fdr.get(change_url(group))
    data = post_data(response)
    count = int(data["group_teams-TOTAL_FORMS"])
    data[f"group_teams-{count}-team"] = str(newcomer.pk)
    data[f"group_teams-{count}-group"] = str(group.pk)
    data["group_teams-TOTAL_FORMS"] = str(count + 1)
    response = admin_client_fdr.post(change_url(group), data)
    assert response.status_code == 302, response.content.decode()[:2000]
    assert GroupTeam.objects.filter(group=group, team=newcomer).exists()
    assert Standing.objects.filter(group=group, team=newcomer, kind="live").exists()
    assert Outbox.objects.filter(id__gt=mark, topic="standings").exists()


# --- Partida e eventos ----------------------------------------------------------------------------


def test_match_edit_goes_through_write_path(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico, round=league["rounds"][0])
    finish(match, operator_user, 2, 1)
    match.refresh_from_db()
    version = match.version
    mark = last_outbox_id()
    response = admin_client_fdr.get(change_url(match))
    page = response.content.decode()
    assert "Cadastrar escalação" in page
    data = post_data(response)
    data["venue"] = "Ilha do Retiro"
    data["attendance"] = "25000"
    data["partial_info"] = "on"  # o check de informações parciais também é gravado
    response = admin_client_fdr.post(change_url(match), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    match.refresh_from_db()
    assert (match.venue, match.attendance, match.partial_info) == ("Ilha do Retiro", 25000, True)
    assert (match.home_score, match.away_score, match.status) == (2, 1, "finished")  # cache intacto
    assert match.version == version + 1
    message = Outbox.objects.filter(id__gt=mark, topic="match").last().payload
    assert message["match"]["venue"] == "Ilha do Retiro"
    assert message["match"]["attendance"] == 25000
    assert AuditLog.objects.filter(action="match.edit", match_id=match.id).exists()


def test_match_inline_change_publishes_match(admin_client_fdr, league):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    mark = last_outbox_id()
    data = post_data(admin_client_fdr.get(change_url(match)))
    data["officials-TOTAL_FORMS"] = "1"
    data.update({"officials-0-order": "1", "officials-0-role": "referee", "officials-0-name": "Fulano de Tal", "officials-0-state": "PE", "officials-0-match": str(match.pk)})
    response = admin_client_fdr.post(change_url(match), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    message = Outbox.objects.filter(id__gt=mark, topic="match").last().payload
    assert message["match"]["officials"][0]["name"] == "Fulano de Tal"


def test_match_event_inline_is_read_only(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    finish(match, operator_user, 1, 0)
    response = admin_client_fdr.get(change_url(match))
    inline = next(item for item in response.context["inline_admin_formsets"] if item.formset.model is MatchEvent)
    assert not inline.has_add_permission and not inline.has_delete_permission
    assert [field for field in inline.formset.form.base_fields] == ["void"]  # só a caixa de cancelar
    assert len(inline.formset.forms) == MatchEvent.objects.filter(match=match).count()


def test_lineup_admin_publishes_match_and_entries_are_names(admin_client_fdr, league):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    mark = last_outbox_id()
    add = admin_client_fdr.get(url(MatchLineup, "add") + f"?match={match.pk}&team={sport.pk}")
    data = post_data(add)
    data.update({"formation": "4-4-2", "coach": "Professor"})
    data.update({"entries-TOTAL_FORMS": "1", "entries-0-name": "", "entries-0-number": "9", "entries-0-starter": "on", "entries-0-order": "1"})
    response = admin_client_fdr.post(url(MatchLineup, "add"), data)
    assert response.status_code == 200  # nome obrigatório (jogador não tem cadastro)
    assert not MatchLineup.objects.filter(match=match).exists()
    data["entries-0-name"] = "Titular"
    response = admin_client_fdr.post(url(MatchLineup, "add"), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    assert response["Location"] == change_url(match)  # "Salvar" volta para a partida
    message = Outbox.objects.filter(id__gt=mark, topic="match").last().payload
    assert message["match"]["lineups"]["home"]["starters"][0] == {"name": "Titular", "number": 9, "position": None}


# --- Auditoria das ações do admin ---------------------------------------------------------------


def test_admin_actions_are_audited(admin_client_fdr, admin_user_fdr):
    response = admin_client_fdr.get(url(Team, "add"))
    data = post_data(response)
    data.update({"name": "Íbis", "short_name": "IBI", "city": "Paulista", "color_primary": "#000000", "color_secondary": "#d7141a"})
    response = admin_client_fdr.post(url(Team, "add"), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    team = Team.objects.get(name="Íbis")
    assert team.color_secondary == "#D7141A"
    added = AuditLog.objects.get(action="admin.add", object_type="competitions.team", object_id=str(team.pk))
    assert added.actor == admin_user_fdr
    assert added.data["object_repr"] == "Íbis"

    data = post_data(admin_client_fdr.get(change_url(team)))
    data["city"] = "Paulista, PE"
    assert admin_client_fdr.post(change_url(team), data).status_code == 302
    changed = AuditLog.objects.get(action="admin.change", object_type="competitions.team", object_id=str(team.pk))
    assert "Cidade" in changed.data["message"] or "cidade" in changed.data["message"]

    other = Team.objects.create(name="Jaguar", short_name="JAG")
    third = Team.objects.create(name="Decisão", short_name="DEC")
    response = admin_client_fdr.post(
        url(Team), {"action": "delete_selected", "_selected_action": [str(other.pk), str(third.pk)], "post": "yes"}
    )
    assert response.status_code == 302
    deleted = AuditLog.objects.filter(action="admin.delete", object_type="competitions.team")
    assert set(deleted.values_list("object_id", flat=True)) == {str(other.pk), str(third.pk)}


def test_log_entry_signal_records_audit(admin_user_fdr, league):
    team = league["teams"][0]
    LogEntry.objects.log_actions(admin_user_fdr.pk, [team], 3)
    entry = AuditLog.objects.get(action="admin.delete", object_id=str(team.pk))
    assert entry.object_type == "competitions.team"
    assert entry.actor_username == admin_user_fdr.username


def test_tie_admin_validates_through_model_clean(admin_client_fdr, league):
    knockout = make_knockout(league["season"], legs=2, extra_time=True)
    response = admin_client_fdr.get(change_url(knockout["tie"]))
    data = post_data(response)
    data["team_b"] = data["team_a"]
    response = admin_client_fdr.post(change_url(knockout["tie"]), data)
    assert response.status_code == 200
    assert "precisam ser diferentes" in response.content.decode()


def test_zone_and_criterion_models_have_no_standalone_admin():
    """Critérios e zonas só existem dentro da fase (validados juntos)."""
    assert not admin.site.is_registered(StageCriterion)
    assert not admin.site.is_registered(StandingZone)
    assert admin.site.is_registered(Group)


def test_changelists_have_no_n_plus_one(admin_client_fdr, league, operator_user):
    """O número de consultas das listas não cresce com o número de linhas."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    sport, nautico, santa, retro = league["teams"]
    models = (Match, Stage, Group, Team, Tie, MatchLineup, AuditLog)

    def add_rows(home, away):
        match = make_match(league["stage"], home, away, round=league["rounds"][0])
        finish(match, operator_user, 1, 1)
        MatchLineup.objects.create(match=match, team=home, formation="4-4-2")
        knockout = make_knockout(league["season"], legs=1, extra_time=False, team_a=home, team_b=away)
        make_tie_matches(knockout["tie"])

    def counts():
        result = {}
        for model in models:
            with CaptureQueriesContext(connection) as ctx:
                assert admin_client_fdr.get(url(model)).status_code == 200
            result[model.__name__] = len(ctx.captured_queries)
        return result

    add_rows(sport, nautico)
    before = counts()
    add_rows(santa, retro)
    add_rows(retro, sport)
    assert counts() == before


# --- Revisão: perfis em todas as páginas -------------------------------------------------------


def _expected_status(model_admin, user, view):
    class FakeRequest:
        pass

    request = FakeRequest()
    request.user = user
    if view == "add":
        return 200 if model_admin.has_add_permission(request) else 403
    return 200 if model_admin.has_view_or_change_permission(request) else 403


@pytest.mark.parametrize("profile", ["operator", "admin"])
def test_every_admin_page_follows_the_profile(profile, operator_user, admin_user_fdr, full_data):
    """Cada lista, inclusão e alteração de todo modelo registrado responde conforme o
    perfil: o Operador cadastra e opera; usuários, grupos, chaves, auditoria e outbox
    são só do Administrador."""
    user = operator_user if profile == "operator" else admin_user_fdr
    client = Client()
    client.force_login(user)
    from django.contrib.auth.models import Group as AuthGroup
    from public_api.models import ApiKey

    admin_only = {get_user_model(), AuthGroup, AuditLog, Outbox, ApiKey}
    for model, model_admin in admin.site._registry.items():
        expected = _expected_status(model_admin, user, "changelist")
        if profile == "operator" and model in admin_only:
            assert expected == 403, model
        assert client.get(url(model)).status_code == expected, (profile, model)
        assert client.get(url(model, "add")).status_code == _expected_status(model_admin, user, "add"), (profile, model)
        obj = model._default_manager.order_by("-pk").first()
        if obj is not None:
            assert client.get(change_url(obj)).status_code == expected, (profile, model)


# --- Revisão: exclusões ------------------------------------------------------------------------


def test_deleting_a_group_takes_its_standing_cache_and_republishes(admin_client_fdr, roles):
    data = make_league(n_teams=2)
    stage = make_stage(data["season"], Stage.Format.GROUPS, name="Grupos", position=2)
    group_a = Group.objects.create(stage=stage, name="Grupo A")
    group_b = Group.objects.create(stage=stage, name="Grupo B")
    GroupTeam.objects.create(group=group_a, team=data["teams"][0])
    GroupTeam.objects.create(group=group_b, team=data["teams"][1])
    recompute_group(group_a)
    recompute_group(group_b)
    page = admin_client_fdr.get(url(Group, "delete", group_b.pk))
    assert page.status_code == 200 and not page.context["perms_lacking"]  # a classificação é cache
    mark = last_outbox_id()
    response = admin_client_fdr.post(url(Group, "delete", group_b.pk), {"post": "yes"})
    assert response.status_code == 302
    assert not Group.objects.filter(pk=group_b.pk).exists()
    message = Outbox.objects.filter(id__gt=mark, topic="standings").last().payload
    assert [group["name"] for group in message["standings"]["groups"]] == ["Grupo A"]


def test_match_with_events_cannot_be_deleted(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    finish(match, operator_user, 1, 0)
    page = admin_client_fdr.get(url(Match, "delete", match.pk))
    assert page.context["perms_lacking"]  # eventos não se apagam
    admin_client_fdr.post(url(Match, "delete", match.pk), {"post": "yes"})
    assert Match.objects.filter(pk=match.pk).exists()


# --- Revisão: fase -------------------------------------------------------------------------------


def test_new_knockout_stage_saves_no_criteria(admin_client_fdr, league):
    data = post_data(admin_client_fdr.get(url(Stage, "add")))
    data.update({"season": league["season"].pk, "name": "Mata-mata", "position": "3", "format": "knockout"})
    response = admin_client_fdr.post(url(Stage, "add"), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    stage = Stage.objects.get(name="Mata-mata")
    assert not stage.criteria.exists() and not stage.groups.exists()


def test_stage_format_is_locked_once_it_has_matches(admin_client_fdr, league, operator_user):
    stage = league["stage"]
    make_match(stage, *league["teams"][:2])
    data = stage_form(admin_client_fdr, stage)
    data["format"] = "knockout"
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200
    assert "A fase já tem partidas: o formato não muda mais." in response.content.decode()
    stage.refresh_from_db()
    assert stage.format == "league"


def test_stage_format_change_without_matches_follows_the_new_format(admin_client_fdr, roles):
    comp_data = make_league(n_teams=2)
    stage = make_stage(comp_data["season"], Stage.Format.LEAGUE, name="Returno", position=2)
    data = stage_form(admin_client_fdr, stage)
    data["format"] = "knockout"
    assert admin_client_fdr.post(change_url(stage), data).status_code == 302
    stage.refresh_from_db()
    assert stage.format == "knockout" and not stage.groups.exists() and not stage.criteria.exists()

    data = stage_form(admin_client_fdr, stage)  # mata-mata: só rodadas na página
    data["format"] = "league"
    mark = last_outbox_id()
    assert admin_client_fdr.post(change_url(stage), data).status_code == 302
    stage.refresh_from_db()
    assert stage.groups.count() == 1
    assert list(stage.criteria.order_by("position").values_list("key", flat=True)) == list(Rules().criteria)
    assert Outbox.objects.filter(id__gt=mark, topic="standings").exists()


def test_season_inline_cannot_change_format_of_stage_with_matches(admin_client_fdr, league):
    stage = league["stage"]
    make_match(stage, *league["teams"][:2])
    data = post_data(admin_client_fdr.get(change_url(league["season"])))
    data["stages-0-format"] = "groups"
    response = admin_client_fdr.post(change_url(league["season"]), data)
    assert response.status_code == 200
    assert "o formato não muda mais" in response.content.decode()


# --- Revisão: grupo e rodada --------------------------------------------------------------------


def test_league_has_a_single_group(admin_client_fdr, league):
    data = post_data(admin_client_fdr.get(url(Group, "add")))
    data.update({"stage": league["stage"].pk, "name": "Outro grupo"})
    response = admin_client_fdr.post(url(Group, "add"), data)
    assert response.status_code == 200
    assert "grupo único automático" in response.content.decode()
    assert league["stage"].groups.count() == 1


def test_team_with_matches_cannot_leave_the_group(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    finish(make_match(league["stage"], sport, nautico), operator_user, 1, 0)
    group = league["group"]
    data = post_data(admin_client_fdr.get(change_url(group)))
    index = next(n for n in range(4) if data[f"group_teams-{n}-team"] == str(sport.pk))
    data[f"group_teams-{index}-DELETE"] = "on"
    response = admin_client_fdr.post(change_url(group), data)
    assert response.status_code == 200
    assert "Time com partidas neste grupo não sai dele: Sport." in response.content.decode()
    assert GroupTeam.objects.filter(group=group, team=sport).exists()
    # Trocar o time da linha também é tirá-lo do grupo.
    data[f"group_teams-{index}-DELETE"] = ""
    data[f"group_teams-{index}-team"] = str(Team.objects.create(name="Central", short_name="CEN").pk)
    response = admin_client_fdr.post(change_url(group), data)
    assert "Time com partidas neste grupo não sai dele: Sport." in response.content.decode()


def test_group_with_matches_keeps_its_stage(admin_client_fdr, league, roles):
    other = make_stage(league["season"], Stage.Format.GROUPS, name="Grupos", position=2)
    make_match(league["stage"], *league["teams"][:2])
    group = league["group"]
    data = post_data(admin_client_fdr.get(change_url(group)))
    data["stage"] = str(other.pk)
    response = admin_client_fdr.post(change_url(group), data)
    assert response.status_code == 200
    assert "O grupo já tem partidas" in response.content.decode()


def test_round_with_matches_keeps_its_stage(admin_client_fdr, league, roles):
    other = make_stage(league["season"], Stage.Format.GROUPS, name="Grupos", position=2)
    rnd = league["rounds"][0]
    make_match(league["stage"], *league["teams"][:2], round=rnd)
    data = post_data(admin_client_fdr.get(change_url(rnd)))
    data["stage"] = str(other.pk)
    response = admin_client_fdr.post(change_url(rnd), data)
    assert response.status_code == 200
    assert "A rodada já tem partidas" in response.content.decode()


# --- Revisão: partida e confronto ---------------------------------------------------------------


def test_match_team_change_that_breaks_events_is_rejected(admin_client_fdr, league, operator_user):
    sport, nautico, santa = league["teams"][:3]
    match = make_match(league["stage"], sport, nautico)
    finish(match, operator_user, 1, 0)
    data = post_data(admin_client_fdr.get(change_url(match)))
    data["home_team"] = str(santa.pk)
    response = admin_client_fdr.post(change_url(match), data)
    assert response.status_code == 200
    assert "os lançamentos já feitos nesta partida ficam inválidos" in response.content.decode()
    match.refresh_from_db()
    assert match.home_team_id == sport.pk and match.home_score == 1


def test_match_home_away_swap_recomputes_from_events(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    finish(match, operator_user, 2, 0)
    data = post_data(admin_client_fdr.get(change_url(match)))
    data["home_team"], data["away_team"] = str(nautico.pk), str(sport.pk)
    response = admin_client_fdr.post(change_url(match), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    match.refresh_from_db()
    assert (match.home_team_id, match.home_score, match.away_score) == (nautico.pk, 0, 2)
    rows = {row.team_id: row for row in Standing.objects.filter(group=league["group"], kind="official")}
    assert (rows[sport.pk].won, rows[nautico.pk].lost) == (1, 1)


def test_match_teams_must_belong_to_the_group(admin_client_fdr, league):
    outsider = Team.objects.create(name="Central", short_name="CEN")
    data = post_data(admin_client_fdr.get(url(Match, "add")))
    data.update(
        {
            "stage": league["stage"].pk,
            "group": league["group"].pk,
            "home_team": league["teams"][0].pk,
            "away_team": outsider.pk,
            "kickoff_at_0": "2026-10-10",
            "kickoff_at_1": "16:00:00",
        }
    )
    response = admin_client_fdr.post(url(Match, "add"), data)
    assert response.status_code == 200
    assert "Central não está na fase “1ª fase”: cadastre o time na tabela da fase antes." in response.content.decode()


@pytest.fixture
def two_groups(league):
    """2ª fase, de grupos: Sport no A; Santa Cruz e Retrô no B; Náutico fora da fase."""
    sport, _nautico, santa_cruz, retro = league["teams"]
    stage = make_stage(league["season"], Stage.Format.GROUPS, name="Grupos", position=2)
    group_a = Group.objects.create(stage=stage, name="Grupo A")
    group_b = Group.objects.create(stage=stage, name="Grupo B")
    for group, team in ((group_a, sport), (group_b, santa_cruz), (group_b, retro)):
        GroupTeam.objects.create(group=group, team=team)
    return {"stage": stage, "groups": (group_a, group_b), "teams": league["teams"]}


def add_match(client, stage, group, home, away):
    data = post_data(client.get(url(Match, "add")))
    data.update(
        {
            "stage": stage.pk,
            "group": group.pk,
            "home_team": home.pk,
            "away_team": away.pk,
            "kickoff_at_0": "2026-10-10",
            "kickoff_at_1": "16:00:00",
        }
    )
    return client.post(url(Match, "add"), data)


def test_match_between_groups_is_accepted_and_counts_for_both(admin_client_fdr, two_groups, operator_user):
    group_a, group_b = two_groups["groups"]
    sport, _nautico, santa_cruz, _retro = two_groups["teams"]
    response = add_match(admin_client_fdr, two_groups["stage"], group_a, sport, santa_cruz)
    assert response.status_code == 302, response.content.decode()[:3000]
    match = Match.objects.get(stage=two_groups["stage"])
    assert match.group_id == group_a.pk  # o grupo do mandante, como na tabela importada
    finish(match, operator_user, 1, 0)
    rows = Standing.objects.filter(group__stage=two_groups["stage"], kind="official", played=1)
    assert {(row.group_id, row.team_id): (row.won, row.lost) for row in rows} == {
        (group_a.pk, sport.pk): (1, 0),
        (group_b.pk, santa_cruz.pk): (0, 1),
    }


def test_match_team_outside_the_stage_or_group_not_of_the_home_team_is_rejected(admin_client_fdr, two_groups):
    group_a, group_b = two_groups["groups"]
    sport, nautico, santa_cruz, _retro = two_groups["teams"]
    response = add_match(admin_client_fdr, two_groups["stage"], group_a, sport, nautico)
    assert response.status_code == 200
    assert "Náutico não está na fase “Grupos”: cadastre o time num grupo da fase antes." in response.content.decode()
    response = add_match(admin_client_fdr, two_groups["stage"], group_b, sport, santa_cruz)
    assert response.status_code == 200
    assert "O jogo fica no grupo do mandante: escolha “Grupo A”, o grupo de Sport." in response.content.decode()
    assert not Match.objects.filter(stage=two_groups["stage"]).exists()


def test_team_swap_in_finished_match_between_groups_recomputes_the_group_it_left(
    admin_client_fdr, two_groups, operator_user
):
    """Visitante trocado num jogo entre grupos já encerrado: o grupo do time que saiu (que
    não é o do jogo nem o antigo) também é recalculado, e a mensagem `standings` sai certa."""
    group_a, group_b = two_groups["groups"]
    sport, nautico, santa_cruz, _retro = two_groups["teams"]
    group_c = Group.objects.create(stage=two_groups["stage"], name="Grupo C")
    GroupTeam.objects.create(group=group_c, team=nautico)
    assert add_match(admin_client_fdr, two_groups["stage"], group_a, sport, santa_cruz).status_code == 302
    match = Match.objects.get(stage=two_groups["stage"])
    finish(match, operator_user, 1, 0)
    assert Standing.objects.get(group=group_b, team=santa_cruz, kind="official").played == 1
    mark = last_outbox_id()
    data = post_data(admin_client_fdr.get(change_url(match)))
    data["away_team"] = str(nautico.pk)
    response = admin_client_fdr.post(change_url(match), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    for kind in ("official", "live"):
        rows = Standing.objects.filter(group__stage=two_groups["stage"], kind=kind, played=1)
        assert {(row.group_id, row.team_id): (row.won, row.lost) for row in rows} == {
            (group_a.pk, sport.pk): (1, 0),
            (group_c.pk, nautico.pk): (0, 1),
        }
    assert Standing.objects.get(group=group_b, team=santa_cruz, kind="official").played == 0
    message = Outbox.objects.filter(id__gt=mark, topic="standings").last().payload
    played = {
        (group["name"], row["team"]["name"]): row["played"]
        for group in message["standings"]["groups"]
        for row in group["rows"]
    }
    assert played[("Grupo B", "Santa Cruz")] == 0
    assert played[("Grupo C", "Náutico")] == 1


def test_tie_change_that_breaks_events_is_rejected(admin_client_fdr, league, operator_user):
    knockout = make_knockout(league["season"], legs=1, extra_time=True)
    match = make_tie_matches(knockout["tie"])[0]
    op = Op(match, operator_user)
    for type_ in ("match_start", "half_time", "second_half_start", "extra_time_start"):
        op.post(type_)
    data = post_data(admin_client_fdr.get(change_url(knockout["tie"])))
    data["extra_time"] = "False"
    response = admin_client_fdr.post(change_url(knockout["tie"]), data)
    assert response.status_code == 200
    assert "ficariam inválidos com esta mudança" in response.content.decode()
    assert Tie.objects.get(pk=knockout["tie"].pk).extra_time is True
    data["team_b"] = str(Team.objects.create(name="Central", short_name="CEN").pk)
    data["extra_time"] = "True"
    response = admin_client_fdr.post(change_url(knockout["tie"]), data)
    assert "os times não mudam" in response.content.decode()


def test_tie_change_republishes_its_matches(admin_client_fdr, league):
    knockout = make_knockout(league["season"], legs=2, extra_time=False)
    legs = make_tie_matches(knockout["tie"])
    mark = last_outbox_id()
    data = post_data(admin_client_fdr.get(change_url(knockout["tie"])))
    data["extra_time"] = "True"
    response = admin_client_fdr.post(change_url(knockout["tie"]), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    published = {row.payload["match"]["id"]: row.payload["match"] for row in Outbox.objects.filter(id__gt=mark, topic="match")}
    assert set(published) == {leg.pk for leg in legs}
    assert all(item["tie"]["extra_time"] is True for item in published.values())


# --- Revisão: consultas das páginas de alteração -------------------------------------------------


def test_change_pages_with_inline_rows_have_no_n_plus_one(admin_client_fdr, league, operator_user):
    """Grupo (times), partida (lances) e escalação (jogadores): o número de consultas
    não cresce com o número de linhas do inline."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    lineup = MatchLineup.objects.create(match=match, team=sport, formation="4-4-2")
    op = Op(match, operator_user)
    op.post("match_start")

    def grow(n):
        for i in range(n):
            team = Team.objects.create(name=f"Clube {Team.objects.count()}", short_name="CLB")
            GroupTeam.objects.create(group=league["group"], team=team)
            MatchLineupPlayer.objects.create(lineup=lineup, name=f"Jogador {lineup.entries.count()}", number=i + 1, position="MF")
            op.post("yellow_card", minute=10 + op.n, team_id=nautico.id, payload={"player": f"Volante {op.n}"})

    def counts():
        result = {}
        for target in (league["group"], match, lineup):
            with CaptureQueriesContext(connection) as ctx:
                assert admin_client_fdr.get(change_url(target)).status_code == 200
            result[type(target).__name__] = len(ctx.captured_queries)
        return result

    grow(2)
    before = counts()
    grow(5)
    assert counts() == before


def test_bulk_delete_of_users_is_audited(admin_client_fdr, admin_user_fdr):
    """Exclusão em lote auditada também nos ModelAdmin que o projeto não declara."""
    User = get_user_model()
    first = User.objects.create_user("temporario1", password="senha-forte-123")
    second = User.objects.create_user("temporario2", password="senha-forte-123")
    response = admin_client_fdr.post(
        url(User), {"action": "delete_selected", "_selected_action": [str(first.pk), str(second.pk)], "post": "yes"}
    )
    assert response.status_code == 302
    deleted = AuditLog.objects.filter(action="admin.delete", object_type="accounts.user")
    assert set(deleted.values_list("object_id", flat=True)) == {str(first.pk), str(second.pk)}
    assert {entry.actor_id for entry in deleted} == {admin_user_fdr.pk}


# --- Partida: jogo repetido no confronto e início editado ------------------------------------


def test_duplicate_tie_leg_in_admin_shows_one_form_error(admin_client_fdr, league):
    """O jogo repetido no confronto é conferido pela restrição `uniq_match_tie_leg` (a
    mensagem aparece uma vez só) com o POST inteiro sob a trava de escrita."""
    knockout = make_knockout(league["season"], legs=1)
    tie = knockout["tie"]
    (existing,) = make_tie_matches(tie)
    response = admin_client_fdr.get(url(Match, "add"))
    data = post_data(response)
    data.update(
        stage=str(tie.stage_id), round=str(tie.round_id), tie=str(tie.pk), leg="1", group="",
        home_team=str(tie.team_b_id), away_team=str(tie.team_a_id),
        kickoff_at_0="2026-10-10", kickoff_at_1="16:00:00",
    )
    response = admin_client_fdr.post(url(Match, "add"), data)
    assert response.status_code == 200
    page = response.content.decode()
    assert page.count("Já existe uma partida para este jogo do confronto.") == 1
    assert list(Match.objects.filter(tie=tie)) == [existing]


def test_admin_kickoff_edit_that_moves_match_off_today_refreshes_latest_goals(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    today = timeutils.local_today()
    noon = timeutils.day_bounds(today)[0] + timedelta(hours=15)
    match = make_match(league["stage"], sport, nautico, kickoff_at=noon, round=league["rounds"][0])
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("goal", minute=3, team_id=sport.id, payload={"player": "Zé"})
    data = post_data(admin_client_fdr.get(change_url(match)))
    tomorrow = timezone.localtime(noon + timedelta(days=1))
    data["kickoff_at_0"] = tomorrow.strftime("%Y-%m-%d")
    mark = last_outbox_id()
    response = admin_client_fdr.post(change_url(match), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    topics = [row.topic for row in Outbox.objects.filter(id__gt=mark).order_by("id")]
    assert topics == ["match", "goals"]
    goals = Outbox.objects.filter(id__gt=mark, topic="goals").get().payload
    assert goals == {"date": today.isoformat(), "changes": [], "latest_goals": []}


# --- Perfis do sistema e acesso ao admin ----------------------------------------------------


def group_url(group, view="change"):
    return reverse(f"admin:auth_group_{view}", args=[group.pk])


def test_system_roles_are_read_only_and_cannot_be_deleted(admin_client_fdr, roles):
    from django.contrib.auth.models import Group as AuthGroup

    operator = roles["operator"]
    before = set(operator.permissions.values_list("pk", flat=True))
    response = admin_client_fdr.get(group_url(operator))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Perfil do sistema: as permissões vêm do código" in page
    assert 'name="permissions"' not in page and 'name="name"' not in page  # nada editável
    assert "matches.post_event" in page  # a lista, somente leitura

    dropped = Permission.objects.get(codename="delete_match")
    response = admin_client_fdr.post(
        group_url(operator), {"name": "Operador renomeado", "permissions": [str(pk) for pk in before - {dropped.pk}]}
    )
    assert response.status_code == 302
    operator.refresh_from_db()
    assert operator.name == "Operador"
    assert set(operator.permissions.values_list("pk", flat=True)) == before

    assert admin_client_fdr.get(group_url(operator, "delete")).status_code == 403
    assert admin_client_fdr.post(group_url(operator, "delete"), {"post": "yes"}).status_code == 403
    bulk = admin_client_fdr.post(
        reverse("admin:auth_group_changelist"),
        {"action": "delete_selected", "_selected_action": [str(operator.pk), str(roles["admin"].pk)], "post": "yes"},
    )
    assert bulk.status_code == 403
    assert AuthGroup.objects.filter(name__in=["Operador", "Administrador"]).count() == 2
    listing = admin_client_fdr.get(reverse("admin:auth_group_changelist")).content.decode()
    assert "Perfil do sistema" in listing


def test_custom_group_stays_editable_and_sync_roles_leaves_it_alone(admin_client_fdr, roles):
    from django.contrib.auth.models import Group as AuthGroup

    from accounts.roles import OPERATOR_PERMISSIONS, sync_roles

    view_match = Permission.objects.get(codename="view_match")
    view_team = Permission.objects.get(codename="view_team")
    response = admin_client_fdr.post(reverse("admin:auth_group_add"), {"name": "Leitor", "permissions": [str(view_match.pk)]})
    assert response.status_code == 302
    reader = AuthGroup.objects.get(name="Leitor")
    page = admin_client_fdr.get(group_url(reader)).content.decode()
    assert 'name="permissions"' in page and "Perfil do sistema" not in page
    response = admin_client_fdr.post(group_url(reader), {"name": "Leitor", "permissions": [str(view_match.pk), str(view_team.pk)]})
    assert response.status_code == 302

    operator = roles["operator"]
    operator.permissions.remove(Permission.objects.get(codename="delete_match"))  # por fora do admin
    sync_roles()
    assert set(reader.permissions.values_list("codename", flat=True)) == {"view_match", "view_team"}
    codes = {f"{perm.content_type.app_label}.{perm.codename}" for perm in operator.permissions.select_related("content_type")}
    assert codes == set(OPERATOR_PERMISSIONS)  # o código continua sendo a fonte dos perfis do sistema
    assert admin_client_fdr.get(group_url(reader, "delete")).status_code == 200


def test_user_put_in_operator_role_through_admin_gets_admin_access(admin_client_fdr, roles):
    User = get_user_model()
    password = "Senha-forte-789"
    response = admin_client_fdr.post(
        url(User, "add"), {"username": "novo.operador", "usable_password": "true", "password1": password, "password2": password}
    )
    assert response.status_code == 302, response.content.decode()[:3000]
    user = User.objects.get(username="novo.operador")
    assert not user.is_staff
    data = post_data(admin_client_fdr.get(change_url(user)))
    data.pop("is_staff", None)  # "Membro da equipe" desmarcado
    data["groups"] = [str(roles["operator"].pk)]
    response = admin_client_fdr.post(change_url(user), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    user.refresh_from_db()
    assert user.is_staff and list(user.groups.values_list("name", flat=True)) == ["Operador"]

    client = Client()
    assert client.login(username="novo.operador", password=password)
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/competitions/competition/").status_code == 200

    # desmarcar "Membro da equipe" com o perfil mantido não tranca o operador fora do admin
    data = post_data(admin_client_fdr.get(change_url(user)))
    data.pop("is_staff", None)
    response = admin_client_fdr.post(change_url(user), data)
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.is_staff
    page = admin_client_fdr.get(change_url(user)).content.decode()
    assert "Operador e Administrador já dão acesso ao Django Admin" in page


def test_joining_a_system_role_in_code_sets_is_staff(roles, django_user_model):
    from django.contrib.auth.models import Group as AuthGroup

    first = django_user_model.objects.create_user("um")
    first.groups.add(roles["operator"])
    assert first.is_staff and django_user_model.objects.get(pk=first.pk).is_staff
    second = django_user_model.objects.create_user("dois")
    roles["admin"].user_set.add(second)
    second.refresh_from_db()
    assert second.is_staff

    custom = AuthGroup.objects.create(name="Leitor")
    third = django_user_model.objects.create_user("tres")
    third.groups.add(custom)
    custom.user_set.add(django_user_model.objects.create_user("quatro"))
    assert not django_user_model.objects.filter(username__in=["tres", "quatro"], is_staff=True).exists()

    first.groups.remove(roles["operator"])  # sair do perfil não desmarca (não tranca ninguém fora)
    first.refresh_from_db()
    assert first.is_staff
