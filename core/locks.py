"""Trava global de escrita.

Toda transação que grava no `outbox` pega antes esta trava (pg_advisory_xact_lock)
e a segura até o commit. Assim os lançamentos entram em fila: sequência, placar,
classificação e últimos gols ficam coerentes, e a ordem dos ids do outbox é a
ordem dos commits.
"""

from django.db import connection, transaction

# Constante arbitrária e estável que identifica a trava de escrita do sistema.
WRITE_LOCK_KEY = 0x46_44_52_01  # "FDR\x01"


def acquire_write_lock():
    """Pega a trava global; precisa estar dentro de `transaction.atomic()`."""
    if not connection.in_atomic_block:
        raise RuntimeError("acquire_write_lock() exige uma transação aberta")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [WRITE_LOCK_KEY])
    # Marca a transação mais externa como dona da trava (vale até o commit).
    connection._fdr_write_lock_block = connection.atomic_blocks[0]


def holds_write_lock() -> bool:
    """True quando a transação corrente já pegou a trava de escrita."""
    if not connection.in_atomic_block or not connection.atomic_blocks:
        return False
    return getattr(connection, "_fdr_write_lock_block", None) is connection.atomic_blocks[0]


def locked_atomic():
    """Context manager: abre transação e pega a trava de escrita."""

    class _Locked:
        def __enter__(self):
            self._atomic = transaction.atomic()
            self._atomic.__enter__()
            try:
                acquire_write_lock()
            except BaseException as exc:  # pragma: no cover - repassa a falha
                self._atomic.__exit__(type(exc), exc, exc.__traceback__)
                raise
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._atomic.__exit__(exc_type, exc, tb)

    return _Locked()
