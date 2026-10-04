"""Django Admin dos cadastros: competições, temporadas, fases, grupos e times — e
as regras da classificação de cada fase (critérios, zonas e punições em pontos).

Navegação por competição: o índice do admin mostra só Competições e Times (e, para o
Administrador, usuários, perfis, chaves e auditoria). Temporada, fase, grupo, rodada,
partida, confronto e escalação somem do índice (`HiddenFromIndexMixin`; os endereços e
as permissões continuam valendo) e são abertos descendo a hierarquia:
Competição › Temporada › Fase › Rodada › Jogo, com essa trilha no topo de cada página
(`HierarchyAdminMixin`, template `admin/fdr/change_form.html`). A rodada e a partida
ficam em matches/admin.py (as listas delas são de partidas e confrontos).

Toda escrita que muda o que as páginas mostram passa pelo mesmo núcleo:

* Fase (pontuação, critérios, zonas): a configuração inteira é validada com
  `standings.domain.validate_rules` nos formulários (o erro aparece em PT-BR junto
  da lista que o causou) e, depois de gravar, `standings.services.on_stage_rules_changed`
  recalcula as tabelas (quando mudou pontuação ou critério) e publica a classificação.
* Times do grupo: `standings.services.recompute_group` e a classificação publicada.
  Time com partidas no grupo não sai dele; pontos corridos tem um grupo só.
* Formato da fase só muda enquanto ela não tem partidas (nem confrontos); mata-mata
  fica sem grupos e fase com tabela ganha os critérios padrão quando não tem nenhum.
  Grupo e rodada com partidas não mudam de fase.
* Punições/bonificações (`standings.PointAdjustment`, inline da fase): o time precisa
  estar num grupo da fase; gravar ou apagar recalcula a fase e publica a classificação
  (`on_stage_rules_changed`).
* Toda gravação e exclusão pega antes a trava de escrita (`BaseAdmin`), como um
  lançamento. A classificação gravada é cache: vai junto quando se apaga fase ou grupo
  (e não tem página no admin: o sistema a recalcula sozinho).

O que cada usuário vê e pode fazer sai das permissões do perfil (accounts/roles.py).
"""

from __future__ import annotations

import re

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AutocompleteSelect
from django.db import transaction
from django.db.models import Count, Q
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from core.locks import locked_atomic
from observability.admin import AuditedModelAdmin
from realtime.outbox import enqueue
from standings import services as standings_services
from standings.domain import CRITERIA, ConfigError, Rules, Zone, validate_rules
from standings.models import PointAdjustment, Standing

from .models import (
    Competition,
    Group,
    GroupTeam,
    Round,
    Season,
    Stage,
    StageCriterion,
    StandingZone,
    Team,
)

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
COLOR_FIELD_NAMES = frozenset({"color", "color_primary", "color_secondary"})
DEFAULT_CRITERIA: tuple[str, ...] = Rules().criteria
CRITERION_CHOICES = [(key, criterion.label) for key, criterion in CRITERIA.items()]
POINTS_FIELDS = frozenset({"points_win", "points_draw", "points_loss", "format"})

# Códigos de `validate_rules` mostrados em cada lista da fase.
CRITERIA_CODES = frozenset({"criteria_empty", "criterion_unknown", "criterion_repeated", "points_negative"})
ZONE_CODES = frozenset({"zone_color_invalid", "zone_range_invalid", "zone_range_inverted", "zones_overlap"})

# Junções para as listas de escolha de chave estrangeira (o __str__ delas segue as FKs).
FK_SELECT_RELATED = {
    Season: ("competition",),
    Stage: ("season__competition",),
    Group: ("stage__season__competition",),
}


# --- Peças comuns -----------------------------------------------------------------------


class ColorInput(forms.TextInput):
    """Seletor de cor nativo (`<input type="color">`); o valor continua `#RRGGBB`."""

    input_type = "color"

    def __init__(self, attrs=None):
        super().__init__({"style": "width:4.5rem;height:2rem;padding:2px;cursor:pointer", **(attrs or {})})


class HexColorField(forms.CharField):
    """Cor `#RRGGBB`, gravada em maiúsculas (o navegador envia `#rrggbb`)."""

    widget = ColorInput

    def to_python(self, value):
        value = super().to_python(value)
        return value.upper() if value else value


class PreloadedAutocompleteSelect(AutocompleteSelect):
    """Autocomplete que mostra o valor já carregado com a linha (`preloaded`), sem a
    consulta que o widget do Django faz a cada linha de um inline."""

    preloaded: dict[str, str] | None = None

    def optgroups(self, name, value, attr=None):
        selected = {str(item) for item in value if str(item) not in self.choices.field.empty_values}
        known = self.preloaded or {}
        if not selected or not selected <= known.keys():
            return super().optgroups(name, value, attr)
        options = [] if self.is_required else [self.create_option(name, "", "", False, 0)]
        for key in sorted(selected):
            options.append(self.create_option(name, key, known[key], selected, len(options)))
        return [(None, options, 0)]


def preload_autocomplete(form) -> None:
    """Passa ao autocomplete de cada campo o objeto já carregado na linha (select_related)."""
    for name, field in form.fields.items():
        widget = getattr(field.widget, "widget", field.widget)
        if not isinstance(widget, PreloadedAutocompleteSelect):
            continue
        model_field = form.instance._meta.get_field(name)
        if model_field.is_cached(form.instance) and getattr(form.instance, name) is not None:
            obj = getattr(form.instance, name)
            widget.preloaded = {str(obj.pk): field.label_from_instance(obj)}


class PreloadedChoicesFormSet(BaseInlineFormSet):
    """Inline cujas linhas já trazem o objeto do autocomplete (uma consulta só por lista)."""

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        preload_autocomplete(form)
        return form


class AdminFormMixin:
    """Cores com seletor nativo e listas de chave estrangeira sem consultas extras."""

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in COLOR_FIELD_NAMES:
            kwargs["form_class"] = HexColorField
            kwargs["widget"] = ColorInput
            field = db_field.formfield(**kwargs)
            field.help_text = field.help_text or "Formato #RRGGBB."
            return field
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        related = FK_SELECT_RELATED.get(db_field.related_model)
        if related and "queryset" not in kwargs:
            kwargs["queryset"] = db_field.related_model._default_manager.select_related(*related)
        if "widget" not in kwargs and db_field.name in self.get_autocomplete_fields(request):
            kwargs["widget"] = PreloadedAutocompleteSelect(db_field, self.admin_site, using=kwargs.get("using"))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class StandingCacheDeletionMixin:
    """A classificação gravada é cache (recalculável): apagar fase, grupo ou competição
    leva as linhas dela junto, sem exigir a permissão de excluir `Standing` (que
    ninguém tem: a tabela não se edita à mão). Eventos continuam protegidos."""

    def get_deleted_objects(self, objs, request):
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        perms_needed.discard(Standing._meta.verbose_name)
        return to_delete, model_count, perms_needed, protected


# --- Navegação por competição ----------------------------------------------------------------

STANDINGS_HELP = (
    "A classificação não tem página própria e ninguém a edita: o sistema a recalcula sozinho "
    "a cada lance, a partir dos jogos desta fase, da pontuação, dos critérios de desempate e das "
    "punições abaixo. Há duas versões: a oficial conta só os jogos encerrados; a ao vivo inclui "
    "também os jogos em andamento — é a que as páginas públicas mostram."
)


class HiddenFromIndexMixin:
    """Fora do índice e da barra lateral do admin: a página é aberta descendo a
    hierarquia (competição › temporada › fase › rodada › jogo). Os endereços, a lista
    do app (/admin/<app>/) e as permissões continuam iguais; só o atalho global some."""

    def get_model_perms(self, request):
        match = getattr(request, "resolver_match", None)
        if match is not None and match.url_name == "app_list":
            return super().get_model_perms(request)
        return {}


def change_url_of(obj) -> str:
    return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])


def hierarchy(obj) -> list[dict]:
    """Trilha Competição › Temporada › Fase › (Grupo | Rodada › (Confronto | Jogo › Escalação))
    até `obj` (inclusive): [{"label", "url"}]."""
    if obj is None:
        return []
    name = obj._meta.model_name
    if name == "competition":
        return [{"label": obj.name, "url": change_url_of(obj)}]
    if name == "season":
        return [*hierarchy(obj.competition), {"label": f"Temporada {obj.year}", "url": change_url_of(obj)}]
    if name == "stage":
        return [*hierarchy(obj.season), {"label": obj.name, "url": change_url_of(obj)}]
    if name in ("group", "round"):
        label = obj.name if name == "group" else obj.label
        return [*hierarchy(obj.stage), {"label": label, "url": change_url_of(obj)}]
    if name == "tie":
        parent = obj.round if obj.round_id else obj.stage
        return [*hierarchy(parent), {"label": f"Confronto: {obj}", "url": change_url_of(obj)}]
    if name == "match":
        parent = obj.round if obj.round_id else (obj.tie.round if obj.tie_id else obj.stage)
        label = f"{obj.home_team.short_name or obj.home_team} × {obj.away_team.short_name or obj.away_team}"
        return [*hierarchy(parent), {"label": f"Jogo {label}", "url": change_url_of(obj)}]
    if name == "matchlineup":
        return [*hierarchy(obj.match), {"label": f"Escalação: {obj.team}", "url": change_url_of(obj)}]
    return [{"label": str(obj), "url": change_url_of(obj)}]


class HierarchyAdminMixin:
    """Página de alteração com a trilha da hierarquia (Início › Competição › … › objeto)
    e "Salvar" voltando para o nível de cima (não para a lista global, que saiu do índice).

    `parent_field` = campo do pai; `parent_model_attr` = (nome do parâmetro GET da página de
    inclusão, modelo do pai) para a trilha da inclusão (ex.: ?season=3 na inclusão da fase)."""

    change_form_template = "admin/fdr/change_form.html"
    parent_params: tuple[tuple[str, type], ...] = ()

    def parent_of(self, obj):
        crumbs = hierarchy(obj)
        return crumbs[-2]["url"] if len(crumbs) > 1 else None

    def add_parent(self, request):
        for param, model in self.parent_params:
            value = request.GET.get(param)
            if value and str(value).isdigit():
                parent = model._default_manager.filter(pk=value).first()
                if parent is not None:
                    return parent
        return None

    def breadcrumbs(self, request, obj) -> list[dict]:
        if obj is not None and obj.pk is not None:
            return hierarchy(obj)
        crumbs = hierarchy(self.add_parent(request))
        return [*crumbs, {"label": f"Adicionar {self.model._meta.verbose_name}", "url": None}]

    def object_tools(self, request, obj) -> list[dict]:
        return []

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context["fdr_breadcrumbs"] = self.breadcrumbs(request, obj)
        context["fdr_object_tools"] = self.object_tools(request, obj) if obj is not None else []
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def _redirect_up(self, request, obj, default):
        if "_continue" in request.POST or "_addanother" in request.POST or "_saveasnew" in request.POST:
            return default
        parent = self.parent_of(obj)
        return HttpResponseRedirect(parent) if parent else default

    def response_post_save_change(self, request, obj):
        return self._redirect_up(request, obj, super().response_post_save_change(request, obj))

    def response_post_save_add(self, request, obj):
        return self._redirect_up(request, obj, super().response_post_save_add(request, obj))


def open_link(obj, label: str = "abrir"):
    """Link "abrir" de uma linha de inline (vazio enquanto a linha não foi salva)."""
    if obj is None or obj.pk is None:
        return "—"
    return format_html('<a href="{}">{}</a>', change_url_of(obj), label)


class BaseAdmin(AdminFormMixin, AuditedModelAdmin):
    """Base dos cadastros. Gravar e excluir pegam antes a trava de escrita (como um
    lançamento): o objeto, os inlines, o recálculo e o outbox entram na mesma fila,
    sem inverter a ordem das travas com um lançamento que grava as mesmas linhas."""

    def save_model(self, request, obj, form, change):
        with locked_atomic():
            super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        with locked_atomic():
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with locked_atomic():
            super().delete_queryset(request, queryset)


class Inline(AdminFormMixin, admin.TabularInline):
    extra = 0


def safe_color(value: str, default: str = "#12306B") -> str:
    return value if isinstance(value, str) and HEX_COLOR_RE.match(value) else default


def color_swatch(value: str):
    return format_html(
        '<span title="{0}" style="display:inline-block;width:1.1em;height:1.1em;border-radius:3px;'
        'vertical-align:middle;border:1px solid rgba(0,0,0,.25);background:{0}"></span> <code>{0}</code>',
        safe_color(value),
    )


def admin_link(obj, label=None):
    url = reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])
    return format_html('<a href="{}">{}</a>', url, label if label is not None else str(obj))


def publish_stage_standings(stage: Stage) -> None:
    """Publica a classificação ao vivo da fase (precisa da trava de escrita)."""
    if stage.has_table:
        enqueue("standings", standings_services.standings_message(stage))


# --- Regras da fase: critérios e zonas validados juntos ------------------------------------


class StageRulesFormSet(BaseInlineFormSet):
    """Lista de critérios ou de zonas da fase.

    As duas listas, com a pontuação do formulário da fase, formam a configuração
    validada por `validate_rules`. Cada lista mostra os erros que são dela; a outra
    lista é lida pelo `rules_registry` (compartilhado pelos formsets da mesma página).
    """

    role = ""
    owned_codes: frozenset[str] = frozenset()

    def __init__(self, *args, rules_registry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rules_registry = rules_registry if rules_registry is not None else {}
        self.rules_registry[self.role] = self

    def _should_delete_form(self, form):
        # Mata-mata não tem tabela: critérios e zonas da página não são gravados
        # (ex.: os critérios padrão pré-preenchidos na inclusão de uma fase de mata-mata).
        if self.instance.format == Stage.Format.KNOCKOUT:
            return True
        return super()._should_delete_form(form)

    # Linhas que ficam: válidas, preenchidas e não marcadas para excluir.
    def kept_rows(self) -> list[dict]:
        rows = []
        for form in self.forms:
            if form.errors or not getattr(form, "cleaned_data", None) or self._should_delete_form(form):
                continue
            rows.append(form.cleaned_data)
        return rows

    def stage_points(self) -> tuple[int, int, int]:
        stage = self.instance
        return (stage.points_win or 0, stage.points_draw or 0, stage.points_loss or 0)

    def sibling(self, role: str) -> StageRulesFormSet | None:
        other = self.rules_registry.get(role)
        if other is not None and other is not self and other.is_bound:
            other.is_valid()  # garante o full_clean da outra lista (sem recursão: o Django guarda o estado)
            return other
        return None

    def criteria_keys(self) -> list[str]:
        criteria = self.sibling("criteria") if self.role != "criteria" else self
        if criteria is None:
            if self.instance.pk is None:
                return []
            return list(self.instance.criteria.order_by("position").values_list("key", flat=True))
        indexed = list(enumerate(criteria.kept_rows()))
        indexed.sort(key=lambda item: (item[1].get("position") is None, item[1].get("position") or 0, item[0]))
        return [row["key"] for _, row in indexed]

    def zones(self) -> list[Zone]:
        zones = self.sibling("zones") if self.role != "zones" else self
        if zones is None:
            if self.instance.pk is None:
                return []
            return standings_services.stage_zones(self.instance)
        return [Zone(row["name"], row["color"], row["position_from"], row["position_to"]) for row in zones.kept_rows()]

    def config_error(self) -> ConfigError | None:
        """Erro da configuração (pontuação, critérios e zonas) que pertence a esta lista.

        `validate_rules` confere critérios e pontuação antes das zonas: a lista de zonas,
        se os critérios estiverem errados, se confere de novo com os critérios padrão
        (assim as duas listas mostram os próprios erros no mesmo envio)."""
        points = self.stage_points()
        criteria = self.criteria_keys()
        zones = self.zones() if self.role == "zones" else []
        for attempt in (criteria, DEFAULT_CRITERIA):
            try:
                validate_rules(points, attempt, zones)
                return None
            except ConfigError as exc:
                if exc.code in self.owned_codes:
                    return exc
                if self.role != "zones" or exc.code in ZONE_CODES:
                    return None
        return None

    def clean(self):
        if any(self.errors):
            return  # cada linha mostra o próprio erro primeiro
        self.prepare_rows()
        if self.instance.format != Stage.Format.KNOCKOUT:  # mata-mata não tem tabela
            error = self.config_error()
            if error is not None:
                raise forms.ValidationError(error.message, code=error.code)
        super().clean()  # ordem repetida (restrição única)

    def prepare_rows(self) -> None:
        pass


class StageCriterionForm(forms.ModelForm):
    key = forms.CharField(label="critério", max_length=32, widget=forms.Select(choices=CRITERION_CHOICES))
    position = forms.IntegerField(
        label="ordem", min_value=1, max_value=999, required=False, help_text="Vazio = depois dos demais."
    )

    class Meta:
        model = StageCriterion
        fields = ("position", "key")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.initial.get("key") or self.instance.key
        if current and current not in CRITERIA:
            # Chave gravada fora do catálogo: aparece como está (e a validação recusa).
            self.fields["key"].widget.choices = [*CRITERION_CHOICES, (current, f"{current} (desconhecido)")]

    def has_changed(self):
        # Fase nova: as linhas pré-preenchidas com os critérios padrão contam como preenchidas.
        return super().has_changed() or (self.instance.pk is None and bool(self.initial.get("key")))


class StageCriterionFormSet(StageRulesFormSet):
    role = "criteria"
    owned_codes = CRITERIA_CODES

    def prepare_rows(self) -> None:
        """Ordem vazia vai para depois das preenchidas, na ordem da tela."""
        rows = [form for form in self.forms if form.cleaned_data and not self._should_delete_form(form)]
        taken = [form.cleaned_data["position"] for form in rows if form.cleaned_data.get("position") is not None]
        next_position = max(taken, default=0) + 1
        for form in rows:
            if form.cleaned_data.get("position") is None:
                form.cleaned_data["position"] = next_position
                form.instance.position = next_position
                next_position += 1

    def save(self, commit=True):
        if commit and self.instance.pk:
            self._release_changed_rows()
        return super().save(commit=commit)

    def _release_changed_rows(self) -> None:
        """Trocar dois critérios de lugar violaria as restrições únicas (fase, ordem) e
        (fase, critério) no meio da gravação: as linhas que mudam (ou saem) recebem
        antes valores provisórios, e a gravação normal escreve os definitivos."""
        pks = [
            form.instance.pk
            for form in self.initial_forms
            if form.instance.pk and (form.has_changed() or self._should_delete_form(form))
        ]
        for n, pk in enumerate(pks, start=1):
            StageCriterion.objects.filter(pk=pk).update(position=10000 + n, key=f"~{n}")


class StandingZoneFormSet(StageRulesFormSet):
    role = "zones"
    owned_codes = ZONE_CODES


class StageCriterionInline(Inline):
    model = StageCriterion
    form = StageCriterionForm
    formset = StageCriterionFormSet
    fields = ("position", "key")
    ordering = ("position",)
    verbose_name = "critério de desempate"
    verbose_name_plural = "critérios de desempate (em ordem)"

    def get_extra(self, request, obj=None, **kwargs):
        return len(DEFAULT_CRITERIA) if obj is None or obj.pk is None else 0


class StandingZoneInline(Inline):
    model = StandingZone
    formset = StandingZoneFormSet
    fields = ("name", "color", "position_from", "position_to")
    ordering = ("position_from",)


class GroupFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        stage = self.instance
        adding = any(form.cleaned_data and not self._should_delete_form(form) and not form.instance.pk for form in self.forms)
        if adding and stage.format == Stage.Format.LEAGUE:
            raise forms.ValidationError("Pontos corridos tem um grupo único automático (“Tabela”): não cadastre grupos.")
        if adding and stage.format == Stage.Format.KNOCKOUT:
            raise forms.ValidationError("Fase de mata-mata não tem grupos.")


class GroupInline(Inline):
    """Grupos da fase, cada um com o link para os times dele. Pontos corridos tem o
    grupo único automático (“Tabela”): sem incluir nem apagar."""

    model = Group
    formset = GroupFormSet
    fields = ("name", "teams_link")
    readonly_fields = ("teams_link",)
    verbose_name_plural = "grupos (os times de cada grupo ficam na página do grupo)"

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(teams_count=Count("group_teams"))

    def has_add_permission(self, request, obj=None):
        if obj is not None and obj.format == Stage.Format.LEAGUE:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.format == Stage.Format.LEAGUE:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description="times")
    def teams_link(self, obj):
        if obj is None or obj.pk is None:
            return "Salve para cadastrar os times."
        return format_html('<a href="{}">Times do grupo ({})</a>', change_url_of(obj), getattr(obj, "teams_count", 0))


class RoundInline(Inline):
    """Rodadas da fase; cada uma abre a página com os jogos (e, no mata-mata, os confrontos)."""

    model = Round
    fields = ("number", "name", "matches_link")
    readonly_fields = ("matches_link",)
    ordering = ("number",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("stage")
            .annotate(matches_count=Count("matches", distinct=True), ties_count=Count("ties", distinct=True))
        )

    @admin.display(description="jogos")
    def matches_link(self, obj):
        if obj is None or obj.pk is None:
            return "Salve para cadastrar os jogos."
        if obj.stage.format == Stage.Format.KNOCKOUT:
            label = f"Confrontos e jogos ({getattr(obj, 'ties_count', 0)} confrontos, {getattr(obj, 'matches_count', 0)} jogos)"
        else:
            label = f"Jogos da rodada ({getattr(obj, 'matches_count', 0)})"
        return format_html('<a href="{}">{}</a>', change_url_of(obj), label)


class PointAdjustmentForm(forms.ModelForm):
    class Meta:
        model = PointAdjustment
        fields = ("team", "points", "reason")

    def clean_points(self):
        points = self.cleaned_data.get("points")
        if points == 0:
            raise forms.ValidationError("Informe um número diferente de zero (negativo tira pontos).")
        return points


class PointAdjustmentInline(Inline):
    """Punição (pontos negativos) ou bonificação (positivos) de um time na fase.
    Gravar ou apagar recalcula as tabelas da fase e publica a classificação."""

    model = PointAdjustment
    form = PointAdjustmentForm
    fields = ("team", "points", "reason", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("created_at", "id")
    verbose_name = "punição ou bonificação"
    verbose_name_plural = "punições e bonificações em pontos (negativo = time perde pontos)"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("team")

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        field = formset.form.base_fields["team"]
        # Só os times dos grupos da fase (a escolha fora deles é recusada), sem atalhos de cadastro.
        field.queryset = Team.objects.filter(group_entries__group__stage=obj).distinct().order_by("name") if obj else Team.objects.none()
        field.choices = list(field.choices)  # lida uma vez para todas as linhas
        for flag in ("can_add_related", "can_change_related", "can_delete_related", "can_view_related"):
            setattr(field.widget, flag, False)
        return formset


# --- Competição, temporada e fase -------------------------------------------------------


class SeasonInline(Inline):
    model = Season
    fields = ("year", "open")
    readonly_fields = ("open",)
    ordering = ("-year",)
    verbose_name_plural = "temporadas"

    @admin.display(description="página")
    def open(self, obj):
        return open_link(obj, "abrir (fases)")


@admin.register(Competition)
class CompetitionAdmin(HierarchyAdminMixin, StandingCacheDeletionMixin, BaseAdmin):
    """Ponto de entrada: competição → temporadas → fases → rodadas → jogos."""

    list_display = ("name", "short_name", "slug", "position", "seasons_list")
    list_editable = ("position",)
    search_fields = ("name", "short_name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SeasonInline]
    readonly_fields = ("stages_panel",)
    fieldsets = (
        (None, {"fields": ("name", "short_name", "slug", "position")}),
        (
            "Fases de cada temporada",
            {
                "fields": ("stages_panel",),
                "description": "Abra a fase para cuidar de grupos, rodadas, jogos e classificação.",
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("seasons")

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        return fieldsets if obj is not None else fieldsets[:1]

    @admin.display(description="temporadas")
    def seasons_list(self, obj):
        return ", ".join(str(season.year) for season in obj.seasons.all()) or "—"

    @admin.display(description="fases")
    def stages_panel(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        seasons = list(obj.seasons.order_by("-year").prefetch_related("stages"))
        if not seasons:
            return "Cadastre uma temporada abaixo e salve: depois é só abrir a temporada para criar as fases."
        add_url = reverse("admin:competitions_stage_add")
        rows = []
        for season in seasons:
            stages = sorted(season.stages.all(), key=lambda stage: (stage.position, stage.pk))
            links = format_html_join(
                " · ",
                "{} <small>({})</small>",
                ((admin_link(stage, stage.name), stage.get_format_display()) for stage in stages),
            )
            rows.append(
                (
                    admin_link(season, f"Temporada {season.year}"),
                    links or "nenhuma fase ainda",
                    format_html('<a href="{}?season={}">+ adicionar fase</a>', add_url, season.pk),
                )
            )
        items = format_html_join("", '<li style="margin:0 0 .4em">{}: {} · {}</li>', rows)
        return format_html('<ul style="margin:0;padding-left:1.1em">{}</ul>', items)


def format_change_problem(stage: Stage, new_format: str) -> str | None:
    """Por que a fase gravada não pode passar a `new_format` (None = pode).

    O formato decide se a fase tem tabela (grupos) ou confrontos: mudar depois de
    cadastradas as partidas deixaria partidas de grupo no mata-mata (ou o contrário)."""
    if stage.pk is None or new_format == stage.format:
        return None
    if stage.matches.exists():
        return "A fase já tem partidas: o formato não muda mais."
    if stage.format == Stage.Format.KNOCKOUT and stage.ties.exists():
        return "A fase tem confrontos cadastrados: apague-os antes de mudar o formato."
    if new_format == Stage.Format.KNOCKOUT and GroupTeam.objects.filter(group__stage=stage).exists():
        return "A fase tem times nos grupos: tire-os antes de transformá-la em mata-mata (mata-mata não tem grupos)."
    if new_format == Stage.Format.KNOCKOUT and stage.point_adjustments.exists():
        return "A fase tem punições em pontos: apague-as antes de transformá-la em mata-mata (mata-mata não tem tabela)."
    if new_format == Stage.Format.LEAGUE and stage.groups.count() > 1:
        return "Pontos corridos tem um grupo só: apague os grupos a mais antes de mudar o formato."
    return None


def apply_stage_format(stage: Stage) -> None:
    """Depois de gravar a fase: mata-mata fica sem grupos (vazios, já conferidos) e fase
    com tabela sem critérios ganha os critérios padrão do plano."""
    if not stage.has_table:
        Group.objects.filter(stage=stage).delete()
        return
    if not stage.criteria.exists():
        StageCriterion.objects.bulk_create(
            StageCriterion(stage=stage, position=position, key=key) for position, key in enumerate(DEFAULT_CRITERIA, start=1)
        )


class StageFormatMixin:
    def clean_format(self):
        new_format = self.cleaned_data.get("format")
        problem = format_change_problem(self.instance, new_format) if new_format else None
        if problem:
            raise forms.ValidationError(problem, code="format_locked")
        return new_format


class StageForm(StageFormatMixin, forms.ModelForm):
    class Meta:
        model = Stage
        fields = ("season", "name", "position", "format", "points_win", "points_draw", "points_loss")


class StageInlineForm(StageFormatMixin, forms.ModelForm):
    class Meta:
        model = Stage
        fields = ("name", "position", "format")


class StageInline(Inline):
    """Fases da temporada. Pontuação, critérios, zonas, grupos e rodadas ficam na página da fase."""

    model = Stage
    form = StageInlineForm
    fields = ("name", "position", "format", "points_summary", "open")
    readonly_fields = ("points_summary", "open")
    ordering = ("position", "id")

    @admin.display(description="pontuação (V/E/D)")
    def points_summary(self, obj):
        if obj is None or obj.pk is None:
            return "3/1/0 (padrão)"
        return f"{obj.points_win}/{obj.points_draw}/{obj.points_loss}"

    @admin.display(description="página")
    def open(self, obj):
        return open_link(obj, "abrir (grupos, rodadas, jogos)")


@admin.register(Season)
class SeasonAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, StandingCacheDeletionMixin, BaseAdmin):
    list_display = ("__str__", "competition", "year")
    list_filter = ("competition",)
    search_fields = ("competition__name", "competition__short_name", "year")
    list_select_related = ("competition",)
    autocomplete_fields = ("competition",)
    inlines = [StageInline]
    parent_params = (("competition", Competition),)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("competition")

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model is not Stage:
            return
        # Fase com tabela criada aqui nasce com os critérios padrão do plano; fase que
        # mudou de formato segue as regras dele. A classificação das fases alteradas
        # (o nome da fase vai nela) é republicada pelo mesmo núcleo da página da fase.
        for stage in formset.new_objects:
            apply_stage_format(stage)
        for stage, changed in formset.changed_objects:
            apply_stage_format(stage)
            if stage.has_table:
                standings_services.on_stage_rules_changed(stage, recalc="format" in changed)


class WriteLockedPostMixin:
    """POST do formulário inteiro — validação e gravação — dentro da trava global de
    escrita (`core.locks`), na fila dos lançamentos.

    * As conferências do formulário (jogo repetido no confronto, número de jogos,
      lançamentos refeitos) leem o banco: sem a trava, dois "Salvar" simultâneos (ou o
      clique duplo) passariam os dois; com ela, o segundo vê o primeiro e recebe o erro
      do formulário (o banco ainda recusa, por `uniq_match_tie_leg` e pelos gatilhos).
    * Ordem única de travas: a linha do confronto só é gravada depois da trava global,
      como no caminho dos lançamentos (que reapura o confronto) — sem deadlock.
    """

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST":
            with locked_atomic():
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(Stage)
class StageAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, WriteLockedPostMixin, StandingCacheDeletionMixin, BaseAdmin):
    form = StageForm
    list_display = ("name", "season", "format", "position", "points_summary", "criteria_summary")
    list_filter = ("format", "season__competition", "season__year")
    search_fields = ("name", "season__competition__name", "season__competition__short_name")
    list_select_related = ("season__competition",)
    autocomplete_fields = ("season",)
    readonly_fields = ("standings_link",)
    parent_params = (("season", Season),)
    fieldsets = (
        (None, {"fields": ("season", "name", "position", "format")}),
        (
            "Pontuação",
            {
                "fields": (("points_win", "points_draw", "points_loss"),),
                "description": "Mudar a pontuação, os critérios ou as punições recalcula as tabelas da fase; "
                "mudar zona ou cor só republica a classificação.",
            },
        ),
        ("De onde vem a classificação", {"fields": ("standings_link",), "description": STANDINGS_HELP}),
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("season__competition")
        if getattr(request.resolver_match, "url_name", "").endswith("_changelist"):
            queryset = queryset.prefetch_related("criteria")
        return queryset

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None or not obj.has_table:
            return fieldsets[:2]
        return fieldsets

    def get_inlines(self, request, obj):
        if obj is None:
            return [StageCriterionInline, StandingZoneInline, GroupInline, RoundInline]
        if obj.format == Stage.Format.KNOCKOUT:
            return [RoundInline]
        # O uso do dia a dia primeiro (rodadas e grupos); depois as regras da tabela.
        return [RoundInline, GroupInline, PointAdjustmentInline, StageCriterionInline, StandingZoneInline]

    def get_formset_kwargs(self, request, obj, inline, prefix):
        kwargs = super().get_formset_kwargs(request, obj, inline, prefix)
        if isinstance(inline, (StageCriterionInline, StandingZoneInline)):
            # Um registro por página: cada lista lê a outra para validar a configuração inteira.
            kwargs["rules_registry"] = request.__dict__.setdefault("_fdr_stage_rules", {})
        if isinstance(inline, StageCriterionInline) and (obj is None or obj.pk is None):
            kwargs["initial"] = [{"position": n, "key": key} for n, key in enumerate(DEFAULT_CRITERIA, start=1)]
        return kwargs

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        stage = form.instance
        apply_stage_format(stage)
        if not stage.has_table:
            return
        table_changed = any(
            formset.model in (StageCriterion, PointAdjustment)
            and (formset.new_objects or formset.changed_objects or formset.deleted_objects)
            for formset in formsets
        )
        recalc = not change or table_changed or bool(POINTS_FIELDS & set(form.changed_data))
        try:
            standings_services.on_stage_rules_changed(stage, recalc=recalc)
        except ConfigError as exc:  # pragma: no cover - os formulários já validaram
            transaction.set_rollback(True)
            self.message_user(request, f"Configuração da fase recusada, nada foi gravado: {exc.message}", messages.ERROR)

    @admin.display(description="pontuação (V/E/D)")
    def points_summary(self, obj):
        return f"{obj.points_win}/{obj.points_draw}/{obj.points_loss}"

    @admin.display(description="critérios")
    def criteria_summary(self, obj):
        if not obj.has_table:
            return "—"
        keys = [item.key for item in sorted(obj.criteria.all(), key=lambda item: item.position)]
        return ", ".join(CRITERIA[key].label if key in CRITERIA else f"{key}?" for key in keys) or "—"

    @admin.display(description="tabela pública")
    def standings_link(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        competition = obj.season.competition
        url = f"{reverse('competition')}?slug={competition.slug}&stage={obj.pk}"
        return format_html('<a href="{}" target="_blank" rel="noopener">Ver a classificação ao vivo na página da competição</a>', url)


# --- Grupo ------------------------------------------------------------------------------------


class GroupTeamFormSet(PreloadedChoicesFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        group = self.instance
        self._check_leaving_teams(group)
        team_ids = [
            form.cleaned_data["team"].pk
            for form in self.forms
            if form.cleaned_data and form.cleaned_data.get("team") and not self._should_delete_form(form)
        ]
        if not team_ids or not group.stage_id:
            return
        elsewhere = (
            GroupTeam.objects.filter(group__stage_id=group.stage_id, team_id__in=team_ids)
            .exclude(group_id=group.pk)
            .select_related("team", "group")
        )
        clashes = [f"{item.team} (em {item.group.name})" for item in elsewhere]
        if clashes:
            raise forms.ValidationError(f"Time já está em outro grupo desta fase: {', '.join(clashes)}.")

    def _check_leaving_teams(self, group: Group) -> None:
        """Time que sai do grupo (linha excluída ou trocada) não pode ter partidas nele:
        a classificação ignoraria esses jogos e os adversários perderiam os pontos."""
        if group.pk is None:
            return
        leaving = {
            form.initial.get("team")  # o time gravado (a instância já tem o valor novo)
            for form in self.initial_forms
            if form.instance.pk and (self._should_delete_form(form) or "team" in form.changed_data)
        } - {None}
        if not leaving:
            return
        played = (
            Team.objects.filter(pk__in=leaving)
            .filter(Q(home_matches__group=group) | Q(away_matches__group=group))
            .distinct()
            .order_by("name")
        )
        names = [team.name for team in played]
        if names:
            raise forms.ValidationError(
                f"Time com partidas neste grupo não sai dele: {', '.join(names)}. "
                "Mude antes o grupo das partidas (ou apague as que não tiveram lançamentos)."
            )


class GroupTeamInline(Inline):
    model = GroupTeam
    formset = GroupTeamFormSet
    fields = ("team", "lot_order")
    autocomplete_fields = ("team",)
    ordering = ("team__name",)

    def get_queryset(self, request):
        # O texto de cada linha (`GroupTeam.__str__`) segue grupo → fase → temporada → competição.
        return super().get_queryset(request).select_related("team", "group__stage__season__competition")


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("stage", "name")

    def clean_stage(self):
        stage = self.cleaned_data["stage"]
        group = self.instance
        if stage is None:
            return stage
        if not stage.has_table:
            raise forms.ValidationError("Fase de mata-mata não tem grupos.")
        if stage.format == Stage.Format.LEAGUE and stage.groups.exclude(pk=group.pk).exists():
            raise forms.ValidationError(
                "Pontos corridos tem um grupo único automático (“Tabela”): cadastre os times nele."
            )
        if group.pk and stage.pk != group.stage_id and group.matches.exists():
            raise forms.ValidationError("O grupo já tem partidas: ele não muda de fase.")
        return stage


@admin.register(Group)
class GroupAdmin(HiddenFromIndexMixin, HierarchyAdminMixin, StandingCacheDeletionMixin, BaseAdmin):
    form = GroupForm
    parent_params = (("stage", Stage),)
    list_display = ("name", "stage", "teams_count")
    list_filter = ("stage__season__competition", "stage")
    search_fields = ("name", "stage__name", "stage__season__competition__name")
    list_select_related = ("stage__season__competition",)
    autocomplete_fields = ("stage",)
    inlines = [GroupTeamInline]

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("stage__season__competition")
        if getattr(request.resolver_match, "url_name", "").endswith("_changelist"):
            queryset = queryset.prefetch_related("group_teams")
        return queryset

    @admin.display(description="times")
    def teams_count(self, obj):
        return len(obj.group_teams.all())

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        teams_changed = any(
            formset.model is GroupTeam and (formset.new_objects or formset.changed_objects or formset.deleted_objects)
            for formset in formsets
        )
        stage_changed = change and "stage" in form.changed_data
        if teams_changed or not change or form.changed_data:  # o nome do grupo também vai na tabela
            group = Group.objects.select_related("stage").get(pk=form.instance.pk)
            with locked_atomic():
                standings_services.recompute_group(group)
                publish_stage_standings(group.stage)
                if stage_changed and form.initial.get("stage"):
                    # A fase antiga perdeu o grupo: a tabela dela também muda.
                    publish_stage_standings(Stage.objects.get(pk=form.initial["stage"]))

    def delete_model(self, request, obj):
        with locked_atomic():
            stage = obj.stage
            super().delete_model(request, obj)
            publish_stage_standings(stage)  # a fase perdeu o grupo (e as linhas dele)

    def delete_queryset(self, request, queryset):
        with locked_atomic():
            stages = list(Stage.objects.filter(groups__in=queryset).distinct())
            super().delete_queryset(request, queryset)
            for stage in stages:
                publish_stage_standings(stage)


# --- Time -------------------------------------------------------------------------------------


@admin.register(Team)
class TeamAdmin(BaseAdmin):
    """Times. Jogadores não têm cadastro: o nome é digitado no lance e na escalação."""

    list_display = ("crest_small", "name", "short_name", "city", "primary_swatch", "secondary_swatch")
    list_display_links = ("crest_small", "name")
    search_fields = ("name", "short_name", "city")
    readonly_fields = ("crest_preview",)
    fieldsets = (
        (None, {"fields": ("name", "short_name", "city")}),
        ("Identidade", {"fields": (("color_primary", "color_secondary"), "crest_file", "crest_url", "crest_preview")}),
    )

    @staticmethod
    def _crest(obj, size: int):
        if obj.crest_src:
            return format_html(
                '<img src="{}" alt="" width="{}" height="{}" style="object-fit:contain;vertical-align:middle">',
                obj.crest_src,
                size,
                size,
            )
        primary = safe_color(obj.color_primary)
        secondary = safe_color(obj.color_secondary, "#FFFFFF")
        return format_html(
            '<span aria-hidden="true" style="display:inline-flex;align-items:center;justify-content:center;'
            "width:{0}px;height:{0}px;border-radius:5px 5px 50% 50%;background:{1};color:{2};"
            'border:2px solid {2};font:700 {3}px/1 sans-serif;vertical-align:middle">{4}</span>',
            size,
            primary,
            secondary,
            max(8, size // 3),
            obj.short_name or "?",
        )

    @admin.display(description="")
    def crest_small(self, obj):
        return self._crest(obj, 28)

    @admin.display(description="prévia do escudo")
    def crest_preview(self, obj):
        if obj is None or obj.pk is None:
            return "Salve para ver a prévia (sem URL, as páginas desenham o escudo com as cores e a sigla)."
        return self._crest(obj, 64)

    @admin.display(description="cor principal")
    def primary_swatch(self, obj):
        return color_swatch(obj.color_primary)

    @admin.display(description="cor secundária")
    def secondary_swatch(self, obj):
        return color_swatch(obj.color_secondary)
