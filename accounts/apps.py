from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_roles(sender, using="default", verbosity=0, **kwargs):
    from .roles import sync_roles

    sync_roles(using=using, verbosity=verbosity)


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Usuários e permissões"

    def ready(self):
        # Roda depois de cada migrate, quando todas as permissões já existem.
        post_migrate.connect(_sync_roles, dispatch_uid="accounts.sync_roles")
