from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

from . import throttle


class ThrottledAdminAuthenticationForm(AdminAuthenticationForm):
    """Login do admin: diz quando o acesso está bloqueado em vez de "senha incorreta"."""

    def clean(self):
        try:
            return super().clean()
        except ValidationError:
            lock = getattr(self.request, "login_lock", None) if self.request is not None else None
            if lock is not None:
                raise ValidationError(throttle.lock_message(lock), code="login_locked") from None
            raise
