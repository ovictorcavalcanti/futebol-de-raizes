"""Métricas em memória do processo, no formato texto do Prometheus (fase 11).

Um processo ASGI só: um registro em memória basta, sem dependência externa.
Uso: `metrics.inc("fdr_events_posted_total", type="goal")`,
`metrics.set_gauge("fdr_sse_connections", 3)`, `metrics.observe(...)`.
"""

import threading
from collections import defaultdict

_HELP = {
    "fdr_http_requests_total": ("counter", "Requisições HTTP por método, rota e status"),
    "fdr_http_request_duration_seconds": ("summary", "Duração das requisições HTTP"),
    "fdr_events_posted_total": ("counter", "Eventos lançados por tipo e origem"),
    "fdr_events_voided_total": ("counter", "Lançamentos cancelados"),
    "fdr_status_changes_total": ("counter", "Mudanças de status por ação"),
    "fdr_domain_rejections_total": ("counter", "Lançamentos rejeitados por regra"),
    "fdr_outbox_messages_total": ("counter", "Mensagens gravadas no outbox por tópico"),
    "fdr_stream_messages_published_total": ("counter", "Mensagens publicadas pelo hub"),
    "fdr_sse_connections": ("gauge", "Conexões SSE abertas"),
    "fdr_sse_connections_total": ("counter", "Conexões SSE abertas desde o início"),
    "fdr_outbox_lag_seconds": ("gauge", "Atraso entre gravar e publicar a última mensagem"),
    "fdr_public_api_requests_total": ("counter", "Requisições à API pública"),
    "fdr_public_api_throttled_total": ("counter", "Requisições barradas pelo limite de uso"),
    "fdr_audit_records_total": ("counter", "Registros de auditoria por ação"),
    "fdr_login_lockouts_total": ("counter", "Bloqueios de login por força bruta (usuário+IP ou IP)"),
    "fdr_rate_limited_total": ("counter", "Requisições recusadas por limite de acesso por IP"),
    "fdr_metrics_series_dropped_total": ("counter", "Séries novas descartadas pelo teto de séries"),
}

# Teto defensivo de séries do processo: cada combinação nova de rótulos fica na memória
# para sempre. Passado o teto, a série nova é descartada (e contada); as existentes seguem.
MAX_SERIES = 10_000
_DROPPED = ("fdr_metrics_series_dropped_total", ())


def _key(labels: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(float)
        self._gauges = {}
        self._summaries = defaultdict(lambda: [0, 0.0])

    def _admit(self, store, key) -> bool:
        """Com o lock: aceita a série se ela já existe ou se ainda cabe no teto."""
        if key in store or len(self._counters) + len(self._gauges) + len(self._summaries) < MAX_SERIES:
            return True
        self._counters[_DROPPED] += 1
        return False

    def inc(self, name: str, amount: float = 1, **labels):
        key = (name, _key(labels))
        with self._lock:
            if self._admit(self._counters, key):
                self._counters[key] += amount

    def set_gauge(self, name: str, value: float, **labels):
        key = (name, _key(labels))
        with self._lock:
            if self._admit(self._gauges, key):
                self._gauges[key] = value

    def add_gauge(self, name: str, amount: float, **labels):
        key = (name, _key(labels))
        with self._lock:
            if self._admit(self._gauges, key):
                self._gauges[key] = self._gauges.get(key, 0) + amount

    def observe(self, name: str, value: float, **labels):
        key = (name, _key(labels))
        with self._lock:
            if self._admit(self._summaries, key):
                entry = self._summaries[key]
                entry[0] += 1
                entry[1] += value

    def value(self, name: str, **labels) -> float:
        key = (name, _key(labels))
        with self._lock:
            if key in self._counters:
                return self._counters[key]
            return self._gauges.get(key, 0)

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._summaries.clear()

    def render(self) -> str:
        def fmt_labels(labels):
            if not labels:
                return ""
            inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            return "{" + inner + "}"

        lines = []
        with self._lock:
            series = defaultdict(list)
            for (name, labels), value in self._counters.items():
                series[name].append((labels, value))
            for (name, labels), value in self._gauges.items():
                series[name].append((labels, value))
            summaries = dict(self._summaries)
        for name in sorted(series):
            kind, help_text = _HELP.get(name, ("untyped", name))
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in sorted(series[name]):
                lines.append(f"{name}{fmt_labels(labels)} {_num(value)}")
        by_name = defaultdict(list)
        for (name, labels), (count, total) in summaries.items():
            by_name[name].append((labels, count, total))
        for name in sorted(by_name):
            kind, help_text = _HELP.get(name, ("summary", name))
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} summary")
            for labels, count, total in sorted(by_name[name]):
                lines.append(f"{name}_count{fmt_labels(labels)} {count}")
                lines.append(f"{name}_sum{fmt_labels(labels)} {_num(total)}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6f}"


metrics = Registry()
