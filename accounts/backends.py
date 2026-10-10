"""Backend de autenticação com o bloqueio progressivo (accounts/throttle.py).

Fica no lugar do ModelBackend: o login da API e o do Django Admin passam por
aqui. Bloqueado, levanta PermissionDenied — o Django encerra a autenticação,
emite `user_login_failed` (vai para a auditoria) e devolve None. O bloqueio fica
em `request.login_lock` para quem chamou mostrar a mensagem certa (429 na API).
Cada tentativa é aberta antes de conferir a senha e encerrada com o resultado,
para as simultâneas não passarem do limite (ver throttle.begin).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied

from . import throttle


class ThrottledModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if request is None or username is None or password is None:
            return super().authenticate(request, username=username, password=password, **kwargs)
        lock = throttle.begin(request, username)
        if lock is not None:
            request.login_lock = lock
            raise PermissionDenied(throttle.lock_message(lock))
        try:
            user = super().authenticate(request, username=username, password=password, **kwargs)
        except BaseException:
            throttle.release(request, username)
            raise
        # Fora do try: record_* encerram a tentativa mesmo se levantarem (erro no
        # cache); dentro, ela seria encerrada duas vezes.
        if user is None:
            lock = throttle.record_failure(request, username)
            if lock is not None:
                request.login_lock = lock
        else:
            throttle.record_success(request, username)
        return user
