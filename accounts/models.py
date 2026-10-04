from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Usuário do back (operador ou administrador).

    Modelo próprio desde a fase 0: trocar o usuário depois de ter dados é caro.
    Perfis são grupos do Django; cada ação é uma permissão (ver accounts/roles.py).
    """

    class Meta:
        db_table = "users"
        verbose_name = "usuário"
        verbose_name_plural = "usuários"

    def __str__(self):
        return self.get_full_name() or self.username
