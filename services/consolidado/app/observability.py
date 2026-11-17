"""Métricas Prometheus, logs JSON (inclusive eventos de segurança) e correlation id (X-Request-ID)."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

from prometheus_client import Counter, Histogram

SERVICO = "consolidado"
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
_REQUEST_ID_VALIDO = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

HTTP_REQUISICOES = Counter(
    "http_requests_total", "Requisições HTTP", ["service", "method", "route", "status"]
)
HTTP_LATENCIA = Histogram(
    "http_request_duration_seconds",
    "Latência HTTP",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
ACESSOS_NEGADOS = Counter(
    "acessos_negados_total", "Requisições recusadas por autenticação/autorização", ["motivo"]
)
EVENTOS = Counter(
    "consolidado_eventos_total",
    "Eventos consumidos por resultado",
    ["resultado"],  # aplicado | duplicado | invalido | assinatura_invalida | erro
)
DEFASAGEM = Histogram(
    "consolidado_defasagem_segundos",
    "Tempo entre o registro do lançamento e a atualização do consolidado",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
CACHE = Counter("consolidado_cache_total", "Consultas ao cache", ["resultado"])  # hit | miss | erro


class JsonFormatter(logging.Formatter):
    """Uma linha JSON por evento. Campos estruturados vêm de `extra={"campos": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        dados = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": SERVICO,
            "logger": record.name,
            "request_id": request_id_ctx.get(),
            "msg": record.getMessage(),
        }
        campos = getattr(record, "campos", None)
        if isinstance(campos, dict):
            dados.update(campos)
        if record.exc_info:
            dados["exc"] = self.formatException(record.exc_info)
        return json.dumps(dados, ensure_ascii=False, default=str)


def configurar_logs(nivel: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    raiz = logging.getLogger()
    raiz.handlers = [handler]
    raiz.setLevel(nivel)


class ObservabilidadeMiddleware:
    """Middleware ASGI puro (sem BaseHTTPMiddleware): request id, métricas RED por rota."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = ""
        for nome, valor in scope.get("headers", []):
            if nome == b"x-request-id":
                rid = valor.decode("latin-1")
                break
        if not _REQUEST_ID_VALIDO.match(rid):
            rid = str(uuid.uuid4())

        token = request_id_ctx.set(rid)
        inicio = time.perf_counter()
        status = 500

        async def enviar(mensagem):
            nonlocal status
            if mensagem["type"] == "http.response.start":
                status = mensagem["status"]
                cabecalhos = list(mensagem.get("headers", []))
                cabecalhos.append((b"x-request-id", rid.encode("latin-1")))
                mensagem = {**mensagem, "headers": cabecalhos}
            await send(mensagem)

        try:
            await self.app(scope, receive, enviar)
        finally:
            rota = getattr(scope.get("route"), "path", "nao_mapeada")
            metodo = scope.get("method", "-")
            HTTP_REQUISICOES.labels(SERVICO, metodo, rota, str(status)).inc()
            HTTP_LATENCIA.labels(SERVICO, metodo, rota).observe(time.perf_counter() - inicio)
            request_id_ctx.reset(token)
