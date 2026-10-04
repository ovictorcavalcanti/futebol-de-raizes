"""Perfil do sistema dá acesso ao Django Admin.

O plano diz que o perfil (grupo) sozinho define o que a pessoa faz; o Operador
cadastra competições, partidas, confrontos, critérios e zonas no Django Admin, que
também exige `is_staff`. Entrar em Operador ou Administrador marca `is_staff`, por
qualquer caminho: `user.groups.add/set` (inclusive o formulário de usuário do admin,
que grava o usuário e depois os grupos) e `group.user_set.add`.

Sair do perfil não desmarca `is_staff`: poderia trancar fora um superusuário ou
alguém de um grupo personalizado; um usuário da equipe sem permissões só vê o
admin vazio.
"""

from __future__ import annotations

from django.contrib.auth.models import Group

from . import roles


def grant_staff_for_roles(user) -> bool:
    """Marca `is_staff` se o usuário está num perfil do sistema. True se mudou."""
    if user is None or user.pk is None or user.is_staff:
        return False
    if not user.groups.filter(name__in=roles.ROLE_PERMISSIONS).exists():
        return False
    type(user).objects.filter(pk=user.pk, is_staff=False).update(is_staff=True)
    user.is_staff = True
    return True


def groups_changed(sender, instance, action, reverse, model, pk_set, **kwargs):
    """`m2m_changed` de `User.groups` nos dois sentidos (só `post_add`)."""
    if action != "post_add" or not pk_set:
        return
    if not reverse:  # user.groups.add(...): instance = usuário, pk_set = grupos
        if not instance.is_staff and Group.objects.filter(pk__in=pk_set, name__in=roles.ROLE_PERMISSIONS).exists():
            type(instance).objects.filter(pk=instance.pk, is_staff=False).update(is_staff=True)
            instance.is_staff = True
    elif instance.name in roles.ROLE_PERMISSIONS:  # group.user_set.add(...): instance = grupo
        model.objects.filter(pk__in=pk_set, is_staff=False).update(is_staff=True)
