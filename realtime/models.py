from django.db import models


class Outbox(models.Model):
    """Mensagem a publicar no stream, gravada na mesma transação da mudança.

    O id é o id da mensagem SSE; a trava de escrita garante que a ordem dos ids
    é a ordem dos commits. As linhas ficam 24 horas para reenvio na reconexão.
    """

    class Topic(models.TextChoices):
        MATCH = "match", "Partida"
        STANDINGS = "standings", "Classificação"
        GOALS = "goals", "Últimos gols"

    id = models.BigAutoField(primary_key=True)
    topic = models.CharField("tópico", max_length=16, choices=Topic.choices)
    payload = models.JSONField("mensagem")
    created_at = models.DateTimeField("criada em", auto_now_add=True, db_index=True)
    published_at = models.DateTimeField("publicada em", null=True, blank=True)

    class Meta:
        db_table = "outbox"
        ordering = ["id"]
        verbose_name = "mensagem do outbox"
        verbose_name_plural = "outbox"

    def __str__(self):
        return f"#{self.id} {self.topic}"
