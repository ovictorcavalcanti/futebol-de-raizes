from django.apps import AppConfig, apps
from django.db.models.signals import m2m_changed, post_migrate


def _sync_roles(sender, using="default", verbosity=0, **kwargs):
    # post_migrate é enviado uma vez por app; os perfis só são sincronizados no
    # último app com modelos, quando todas as permissões já foram criadas.
    with_models = [config for config in apps.get_app_configs() if config.models_module is not None]
    if not with_models or sender.label != with_models[-1].label:
        return
    from .roles import sync_roles

    sync_roles(using=using, verbosity=verbosity)


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Usuários e permissões"

    def ready(self):
        from .models import User
        from .signals import groups_changed

        post_migrate.connect(_sync_roles, dispatch_uid="accounts.sync_roles")
        # Entrar num perfil do sistema dá acesso ao Django Admin (is_staff).
        m2m_changed.connect(groups_changed, sender=User.groups.through, dispatch_uid="accounts.staff_for_roles")
