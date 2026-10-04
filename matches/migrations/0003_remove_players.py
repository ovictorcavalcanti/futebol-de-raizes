"""Jogadores sem cadastro: o nome fica só no lance (`payload`) e na escalação.

Antes de tirar as chaves estrangeiras para `competitions.Player` (que a migração
`competitions.0003_delete_player` apaga em seguida), copia o nome do jogador cadastrado
para onde ele passa a morar:

* escalação: `MatchLineupPlayer.name` (e o número, se vazio); entrada sem nome nenhum
  (nem digitado, nem cadastrado) não tinha como ser lida e sai;
* lance: `payload.player` (ou `player_out`/`player_in` na substituição) quando só havia o
  id; os ids (`player_out_id`/`player_in_id`) saem do payload, para todo lance ser lido
  pelo nome, igual aos lançamentos novos.
"""

from django.db import migrations, models

ID_KEYS = ("player_id", "player_out_id", "player_in_id")


def copy_names(apps, schema_editor):
    Player = apps.get_model("competitions", "Player")
    MatchEvent = apps.get_model("matches", "MatchEvent")
    MatchLineupPlayer = apps.get_model("matches", "MatchLineupPlayer")
    names = dict(Player.objects.values_list("id", "name"))
    numbers = dict(Player.objects.values_list("id", "number"))

    for entry in MatchLineupPlayer.objects.filter(player__isnull=False):
        changed = []
        if not entry.name:
            entry.name = names.get(entry.player_id, "")
            changed.append("name")
        if entry.number is None and numbers.get(entry.player_id) is not None:
            entry.number = numbers[entry.player_id]
            changed.append("number")
        if changed:
            entry.save(update_fields=changed)
    MatchLineupPlayer.objects.filter(name="").delete()

    candidates = MatchEvent.objects.filter(models.Q(player__isnull=False) | models.Q(payload__has_any_keys=list(ID_KEYS)))
    for event in candidates.iterator():
        payload = dict(event.payload) if isinstance(event.payload, dict) else {}
        before = dict(payload)
        if event.player_id and not payload.get("player") and event.type != "substitution":
            if names.get(event.player_id):
                payload["player"] = names[event.player_id]
        for key, name_key in (("player_out_id", "player_out"), ("player_in_id", "player_in")):
            player_id = payload.get(key)
            if isinstance(player_id, int) and not payload.get(name_key) and names.get(player_id):
                payload[name_key] = names[player_id]
        for key in ID_KEYS:
            payload.pop(key, None)
        if payload != before:
            # Linha de evento é imutável para o sistema; aqui é a migração do formato.
            MatchEvent.objects.filter(pk=event.pk).update(payload=payload)


class Migration(migrations.Migration):
    dependencies = [
        ("competitions", "0002_zone_no_overlap"),
        ("matches", "0002_structure_constraints"),
    ]

    operations = [
        migrations.RunPython(copy_names, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="matchevent",
            name="player",
        ),
        migrations.RemoveField(
            model_name="matchlineupplayer",
            name="player",
        ),
        migrations.AlterField(
            model_name="matchlineupplayer",
            name="name",
            field=models.CharField(max_length=80, verbose_name="nome"),
        ),
    ]
