"""Prévia do link compartilhado de um jogo (/?jogo=<id>, a home no dia dele com o card aberto):
título e descrição com placar, competição e autores dos gols, o mesmo resumo da mensagem do
botão de compartilhar (static/js/share.js). WhatsApp e redes leem as tags `og:` do HTML."""

from datetime import datetime

from core import timeutils

_SCORED = ("live", "finished", "suspended")
_ORIGIN_TAG = {"penalty": "pên.", "own_goal": "contra"}


def _name(team: dict | None) -> str:
    team = team or {}
    return team.get("name") or team.get("short_name") or "?"


def has_score(data: dict) -> bool:
    """Placar à mostra: jogo começado (o cancelado só com gol), como o card."""
    if data["status"] in _SCORED:
        return True
    return data["status"] == "cancelled" and bool(data["home_score"] or data["away_score"])


def scoreline(data: dict) -> str:
    """"Sport 2 × 1 Náutico" (com os pênaltis, se houver) ou "Sport × Náutico"."""
    home, away = _name(data["home"]), _name(data["away"])
    if not has_score(data):
        return f"{home} × {away}"
    line = f"{home} {data['home_score']} × {data['away_score']} {away}"
    if data.get("home_penalties") is not None and data.get("away_penalties") is not None:
        line += f" (pênaltis {data['home_penalties']} × {data['away_penalties']})"
    return line


def scorers(data: dict, side: str) -> str:
    """"Fulano 12', Ciclano 80' (pên.)" de um lado, na ordem do jogo."""
    items = []
    for goal in data.get("goals") or []:
        if goal.get("team_side") != side:
            continue
        text = goal.get("player") or "a confirmar"
        if goal.get("minute_label"):
            text += f" {goal['minute_label']}"
        if goal.get("origin") in _ORIGIN_TAG:
            text += f" ({_ORIGIN_TAG[goal['origin']]})"
        items.append(text)
    return ", ".join(items)


def match_preview(data: dict) -> tuple[str, str]:
    """(título, descrição) de um MatchOut para as tags da prévia."""
    context = [(data.get("competition") or {}).get("name"), (data.get("round") or {}).get("name")]
    if data["status"] == "scheduled" and data.get("kickoff_at"):
        kickoff = datetime.fromisoformat(data["kickoff_at"]).astimezone(timeutils.app_tz())
        context.append(kickoff.strftime("%d/%m às %H:%M"))
    else:
        context.append(data.get("status_label"))
    walkover = data.get("walkover")
    if walkover:
        favored = data["home"] if (data["home"] or {}).get("id") == walkover.get("team_id") else data["away"]
        context.append(f"W.O. em favor do {_name(favored)}")
    goals = [f"{_name(data[side])}: {text}" for side in ("home", "away") if (text := scorers(data, side))]
    description = " · ".join(part for part in context if part)
    if goals:
        description += ". Gols: " + "; ".join(goals)
    return scoreline(data), description
