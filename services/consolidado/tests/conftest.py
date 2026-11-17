from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import base64

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import EventoLancamento, ReadModelIndisponivel, RelatorioDiario
from app.main import Dependencias, create_app

# Par do IdP de teste: a privada fica só aqui (papel do IdP); o serviço recebe só a pública.
IDP_PRIVADA = Ed25519PrivateKey.generate()
IDP_PUBLICA_B64 = base64.b64encode(
    IDP_PRIVADA.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
).decode()


def emitir_token(comerciante: str = "loja-1", escopos: str = "consolidado:read") -> str:
    agora = int(time.time())
    return jwt.encode(
        {
            "sub": "teste",
            "iss": "fluxo-caixa-idp-local",
            "aud": "fluxo-caixa",
            "iat": agora,
            "exp": agora + 300,
            "comerciante_id": comerciante,
            "scope": escopos,
        },
        IDP_PRIVADA,
        algorithm="EdDSA",
    )


def auth(**kw) -> dict:
    return {"Authorization": f"Bearer {emitir_token(**kw)}"}


def evento_json(tipo="CREDITO", valor="10.00", data="2026-11-10", comerciante="loja-1", event_id=None, **extra) -> bytes:
    d = {
        "event_id": str(event_id or uuid4()),
        "event_type": "LancamentoRegistrado",
        "event_version": 1,
        "occurred_at": "2026-11-10T15:00:00+00:00",
        "comerciante_id": comerciante,
        "lancamento_id": str(uuid4()),
        "tipo": tipo,
        "valor": valor,
        "data_competencia": data,
        "estorno_de": None,
    }
    d.update(extra)
    return json.dumps(d).encode()


def evento(**kw) -> EventoLancamento:
    return EventoLancamento.de_json(evento_json(**kw))


class RepositorioEmMemoria:
    """Dublê do Postgres com a mesma semântica: dedupe por event_id + soma por (comerciante, dia)."""

    def __init__(self):
        self.processados: set = set()
        self.saldos: dict[tuple[str, date], dict] = {}
        self.consultas = 0
        self.falhar = False  # banco fora (mesma exceção que o repositório Postgres levanta)
        self.falhas_restantes = 0  # banco fora só nas próximas N tentativas
        self.defeito = False  # erro inesperado, que não é de infraestrutura
        self.tentativas = 0

    async def aplicar_evento(self, ev: EventoLancamento) -> bool:
        self.tentativas += 1
        if self.defeito:
            raise RuntimeError("defeito inesperado")
        if self.falhas_restantes > 0:
            self.falhas_restantes -= 1
            raise ReadModelIndisponivel("banco indisponível")
        if self.falhar:
            raise ReadModelIndisponivel("banco indisponível")
        if ev.event_id in self.processados:
            return False
        self.processados.add(ev.event_id)
        s = self.saldos.setdefault(
            (ev.comerciante_id, ev.data_competencia),
            {"c": Decimal("0.00"), "d": Decimal("0.00"), "q": 0, "t": None},
        )
        s["c"] += ev.credito
        s["d"] += ev.debito
        s["q"] += 1
        s["t"] = datetime(2026, 11, 10, 15, 0, 1, tzinfo=timezone.utc)
        return True

    async def relatorio(self, comerciante_id: str, data: date) -> RelatorioDiario:
        self.consultas += 1
        dia = self.saldos.get((comerciante_id, data))
        acumulado = sum(
            (v["c"] - v["d"] for (c, d), v in self.saldos.items() if c == comerciante_id and d <= data),
            Decimal("0.00"),
        )
        return RelatorioDiario.calcular(
            data=data,
            creditos=dia["c"] if dia else Decimal("0.00"),
            debitos=dia["d"] if dia else Decimal("0.00"),
            quantidade=dia["q"] if dia else 0,
            saldo_acumulado=acumulado,
            atualizado_em=dia["t"] if dia else None,
        )

    async def saudavel(self) -> bool:
        return True


class CacheEmMemoria:
    def __init__(self, quebrado: bool = False):
        self.dados: dict[str, bytes] = {}
        self.quebrado = quebrado

    async def obter(self, chave):
        return None if self.quebrado else self.dados.get(chave)

    async def gravar(self, chave, valor, ttl_s):
        if not self.quebrado:
            self.dados[chave] = valor

    async def fechar(self):
        pass


@pytest.fixture
def repo() -> RepositorioEmMemoria:
    return RepositorioEmMemoria()


@pytest.fixture
def cache() -> CacheEmMemoria:
    return CacheEmMemoria()


@pytest.fixture
def client(repo, cache):
    app = create_app(
        settings=Settings(jwt_chave_publica=IDP_PUBLICA_B64, consumidor_enabled=False),
        dependencias=Dependencias(repo=repo, cache=cache),
    )
    with TestClient(app) as c:
        yield c
