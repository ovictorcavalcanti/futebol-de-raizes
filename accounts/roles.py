"""Perfis (grupos do Django) e as permissões que cada um reúne.

O Operador faz todo o trabalho do dia a dia; o Administrador faz o mesmo e é o
único que gerencia usuários, níveis de acesso, chaves da API pública e auditoria.
Os grupos são recriados de forma idempotente a cada `migrate` (post_migrate).
"""

OPERATOR = "Operador"
ADMINISTRATOR = "Administrador"

# Ações de operação (permissões próprias declaradas em matches.Match.Meta).
OPS_PERMISSIONS = [
    "matches.post_event",
    "matches.void_event",
    "matches.change_status",
]

# Cadastros e regras da classificação: CRUD completo pelo Django Admin.
_CRUD_MODELS = [
    "competitions.competition",
    "competitions.season",
    "competitions.stage",
    "competitions.stagecriterion",
    "competitions.standingzone",
    "competitions.group",
    "competitions.round",
    "competitions.team",
    "competitions.groupteam",
    "competitions.player",
    "matches.tie",
    "matches.match",
    "matches.matchlineup",
    "matches.matchlineupplayer",
    "matches.matchofficial",
    "matches.matchbroadcast",
    "matches.matchstat",
]


def _crud(model_label: str) -> list[str]:
    app, model = model_label.split(".")
    return [f"{app}.{action}_{model}" for action in ("add", "change", "delete", "view")]


OPERATOR_PERMISSIONS = (
    OPS_PERMISSIONS
    + [perm for label in _CRUD_MODELS for perm in _crud(label)]
    + [
        "matches.view_matchevent",
        "standings.view_standing",
    ]
)

ADMIN_EXTRA_PERMISSIONS = (
    _crud("accounts.user")
    + _crud("auth.group")
    + ["auth.view_permission"]
    + _crud("public_api.apikey")
    + ["observability.view_auditlog", "realtime.view_outbox", "observability.view_metrics"]
)

ROLE_PERMISSIONS = {
    OPERATOR: OPERATOR_PERMISSIONS,
    ADMINISTRATOR: OPERATOR_PERMISSIONS + ADMIN_EXTRA_PERMISSIONS,
}


def sync_roles(using="default", verbosity=0):
    """Cria/atualiza os grupos com exatamente as permissões declaradas."""
    from django.contrib.auth.models import Group, Permission

    missing = []
    for role, perms in ROLE_PERMISSIONS.items():
        group, _ = Group.objects.using(using).get_or_create(name=role)
        resolved = []
        for perm in perms:
            app_label, codename = perm.split(".")
            obj = (
                Permission.objects.using(using)
                .filter(content_type__app_label=app_label, codename=codename)
                .first()
            )
            if obj is None:
                missing.append(perm)
                continue
            resolved.append(obj)
        group.permissions.set(resolved)
    if missing and verbosity >= 2:
        print(f"[roles] permissões ainda inexistentes: {sorted(set(missing))}")
    return missing
