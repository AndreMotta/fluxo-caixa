"""Relay do Transactional Outbox: publica no broker os eventos já gravados no banco.

Garantia: at-least-once. Se o broker cair, os eventos ficam pendentes no outbox e são
publicados quando ele voltar. O consumidor (Consolidado) é idempotente por event_id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .assinatura import CABECALHO_ASSINATURA, assinar
from .observability import OUTBOX_FALHAS, OUTBOX_PENDENTES, OUTBOX_PUBLICADOS
from .repository import OutboxStore

logger = logging.getLogger(__name__)


class Publicador(Protocol):
    async def publicar(self, routing_key: str, corpo: bytes, message_id: str) -> None: ...

    async def fechar(self) -> None: ...


class RabbitPublicador:
    """Publica com publisher confirms e mandatory=True.

    Se a fila do Consolidado ainda não existir, a mensagem volta (unroutable) e a publicação
    falha: o evento continua no outbox em vez de ser descartado pelo broker.

    O exchange é criado pela infraestrutura; aqui ele só é verificado (declaração passiva),
    porque o usuário deste serviço só tem permissão de PUBLICAR, não de configurar.

    Cada mensagem sai assinada com a chave privada Ed25519 deste serviço (ver `assinatura.py`).
    """

    def __init__(self, url: str, exchange: str, chave_privada: Ed25519PrivateKey):
        if not isinstance(chave_privada, Ed25519PrivateKey):
            raise ValueError("chave privada Ed25519 dos eventos é obrigatória")
        self._url = url
        self._exchange_nome = exchange
        self._chave = chave_privada
        self._conexao: AbstractRobustConnection | None = None
        self._canal: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def _conectar(self) -> AbstractExchange:
        if self._exchange is None:
            self._conexao = await aio_pika.connect_robust(self._url, timeout=5)
            self._canal = await self._conexao.channel(publisher_confirms=True, on_return_raises=True)
            self._exchange = await self._canal.get_exchange(self._exchange_nome, ensure=True)
        return self._exchange

    async def publicar(self, routing_key: str, corpo: bytes, message_id: str) -> None:
        try:
            exchange = await self._conectar()
            await exchange.publish(
                aio_pika.Message(
                    body=corpo,
                    message_id=message_id,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers={CABECALHO_ASSINATURA: assinar(self._chave, corpo)},
                ),
                routing_key=routing_key,
                mandatory=True,
                timeout=5,
            )
        except Exception:
            await self.fechar()  # força reconexão limpa na próxima tentativa
            raise

    async def fechar(self) -> None:
        conexao, self._conexao, self._canal, self._exchange = self._conexao, None, None, None
        if conexao is not None:
            try:
                await conexao.close()
            except Exception:  # pragma: no cover - melhor esforço no encerramento
                pass


class FalhaPublicacao(Exception):
    """O broker recusou ou não respondeu; o restante do lote fica pendente no outbox."""


async def publicar_pendentes(store: OutboxStore, publicador: Publicador, limite: int) -> int:
    """Publica um lote. Para no primeiro erro e confirma só o que foi publicado.

    A falha é sinalizada DEPOIS de fechar o lote, para não desfazer a marcação
    dos eventos que já foram aceitos pelo broker.
    """
    falha: Exception | None = None
    async with store.lote_pendente(limite) as lote:
        publicados: list[int] = []
        for evento in lote.eventos:
            try:
                await publicador.publicar(evento.routing_key, evento.payload, evento.event_id)
            except Exception as exc:
                falha = exc
                break
            publicados.append(evento.id)
        await lote.marcar_publicados(publicados)
    OUTBOX_PUBLICADOS.inc(len(publicados))
    if falha is not None:
        OUTBOX_FALHAS.inc()
        raise FalhaPublicacao(str(falha)) from falha
    return len(publicados)


async def medir_backlog(store: OutboxStore) -> None:
    """Atualiza o gauge do backlog, inclusive quando a publicação falhou.

    Broker fora do ar é exatamente o cenário em que o alerta `OutboxAcumulando` precisa
    enxergar o backlog crescendo; por isso a medição não pode depender de publicar com sucesso.
    """
    try:
        OUTBOX_PENDENTES.set(await store.contar_pendentes())
    except Exception as exc:
        logger.warning("não foi possível medir o backlog do outbox: %s", exc)


async def executar_relay(
    store: OutboxStore,
    publicador: Publicador,
    *,
    limite: int,
    intervalo_s: float,
    parar: asyncio.Event,
) -> None:
    espera = intervalo_s
    while not parar.is_set():
        try:
            publicados = await publicar_pendentes(store, publicador, limite)
            espera = 0 if publicados == limite else intervalo_s
        except Exception as exc:
            logger.warning("relay do outbox sem publicar (broker indisponível?): %s", exc)
            espera = min(max(espera * 2, intervalo_s), 30)  # backoff até 30 s
        await medir_backlog(store)
        if espera:
            try:
                await asyncio.wait_for(parar.wait(), timeout=espera)
            except asyncio.TimeoutError:
                pass
