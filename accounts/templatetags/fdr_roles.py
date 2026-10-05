"""Filtros de perfil para os templates do admin."""

from django import template

from accounts.roles import is_administrator as _is_administrator

register = template.Library()


@register.filter
def is_administrator(user) -> bool:
    return _is_administrator(user)
