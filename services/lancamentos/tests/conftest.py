from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient

from app.assinatura import carregar_chave_privada, gerar_par
from app.config import Settings
from app.domain import ConflitoIdempotencia, Lancamento, LancamentoJaEstornado
from app.main import Dependencias, create_app
from app.repository import EventoOutbox, ResultadoRegistro
from app.service import Relogio

# Par do IdP de teste: a privada fica só aqui (papel do IdP); o serviço recebe só a pública.
IDP_PRIVADA_B64, IDP_PUBLICA_B64 = gerar_par()
IDP_PRIVADA = carregar_chave_privada(IDP_PRIVADA_B64)
HOJE = date(2026, 11, 10)
AGORA = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def emitir_token(comerciante: str = "loja-1", escopos: str = "lancamentos:write lancamentos:read", **extra) -> str:
    agora = int(time.time())
    claims = {
        "sub": "teste",
        "iss": "fluxo-caixa-idp-local",
        "aud": "fluxo-caixa",
        "iat": agora,
        "exp": agora + 300,
        "comerciante_id": comerciante,
        "scope": escopos,
    }
    claims.update(extra)
    return jwt.encode(claims, IDP_PRIVADA, algorithm="EdDSA")


def cabecalhos(chave: str | None = "chave-0001", **kw) -> dict:
    h = {"Authorization": f"Bearer {emitir_token(**kw)}"}
    if chave:
        h["Idempotency-Key"] = chave
    return h


class _LoteMem:
    def __init__(self, eventos: list[EventoOutbox]):
        self.eventos = eventos
        self.marcados: list[int] = []

    async def marcar_publicados(self, ids: list[int]) -> None:
        self.marcados.extend(ids)


class RepositorioEmMemoria:
    """Dublê do Postgres com as mesmas regras de idempotência, unicidade de estorno e outbox."""

    def __init__(self):
        self.lancamentos: dict[UUID, Lancamento] = {}
        self.chaves: dict[tuple[str, str], tuple[UUID, str]] = {}
        self.outbox: list[EventoOutbox] = []
        self.publicados: set[int] = set()

    async def registrar(self, lanc, evento, chave, hash_req) -> ResultadoRegistro:
        k = (lanc.comerciante_id, chave)
        if k in self.chaves:
            lid, h = self.chaves[k]
            if h != hash_req:
                raise ConflitoIdempotencia(chave)
            return ResultadoRegistro(self.lancamentos[lid], criado=False)
        if lanc.estorno_de and any(l.estorno_de == lanc.estorno_de for l in self.lancamentos.values()):
            raise LancamentoJaEstornado(str(lanc.estorno_de))
        self.lancamentos[lanc.id] = lanc
        self.chaves[k] = (lanc.id, hash_req)
        self.outbox.append(
            EventoOutbox(
                id=len(self.outbox) + 1,
                event_id=str(evento.event_id),
                routing_key=evento.ROUTING_KEY,
                payload=evento.to_json(),
            )
        )
        return ResultadoRegistro(lanc, criado=True)

    async def obter(self, comerciante_id, lancamento_id):
        lanc = self.lancamentos.get(lancamento_id)
        return lanc if lanc and lanc.comerciante_id == comerciante_id else None

    async def listar_por_data(self, comerciante_id, data, limite, deslocamento):
        itens = sorted(
            (l for l in self.lancamentos.values() if l.comerciante_id == comerciante_id and l.data_competencia == data),
            key=lambda l: (l.criado_em, str(l.id)),
        )
        return itens[deslocamento : deslocamento + limite]

    async def saudavel(self) -> bool:
        return True

    # --- OutboxStore ---
    @asynccontextmanager
    async def lote_pendente(self, limite: int):
        lote = _LoteMem([e for e in self.outbox if e.id not in self.publicados][:limite])
        yield lote
        self.publicados.update(lote.marcados)  # "commit": só aplica se não houve exceção

    async def contar_pendentes(self) -> int:
        return len([e for e in self.outbox if e.id not in self.publicados])


@pytest.fixture
def repo() -> RepositorioEmMemoria:
    return RepositorioEmMemoria()


@pytest.fixture
def client(repo):
    app = create_app(
        settings=Settings(jwt_chave_publica=IDP_PUBLICA_B64, outbox_relay_enabled=False),
        dependencias=Dependencias(repo=repo, relogio=Relogio(agora=lambda: AGORA, hoje=lambda: HOJE)),
    )
    with TestClient(app) as c:
        yield c
