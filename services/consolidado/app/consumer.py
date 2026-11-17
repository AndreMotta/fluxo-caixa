"""Consumidor RabbitMQ dos eventos de Lançamentos.

Topologia (criada pela infraestrutura em infra/rabbitmq/topologia.sh, NÃO por este serviço):
  exchange topic  fluxo-caixa.lancamentos  --lancamento.registrado.*-->  fila quorum consolidado.lancamentos
  fila consolidado.lancamentos  --(x-delivery-limit esgotado / reject)-->  DLX -> consolidado.lancamentos.dlq
O usuário do Consolidado só tem permissão de LER a própria fila (não publica nem configura nada).

Ordem de validação de cada mensagem:
  1. assinatura Ed25519 (autenticidade/integridade) -> inválida ou ausente: DLQ + log de segurança
  2. contrato do evento (schema/versão)             -> inválido: DLQ
  3. aplicação no read model
       - banco indisponível: a mensagem fica RETIDA neste processo e a aplicação é repetida com
         backoff até o banco voltar. Não é devolvida à fila, então a queda do banco não consome o
         `x-delivery-limit` e não manda evento válido para a DLQ.
       - erro inesperado (possível defeito ligado à mensagem): requeue; o `x-delivery-limit`
         tira a mensagem do caminho (DLQ) se ela falhar sempre.

Este serviço só tem CHAVES PÚBLICAS: confere a assinatura, mas não consegue assinar. Mesmo
comprometido, ele não tem segredo que permita forjar um evento aceito.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .domain import EventoInvalido, EventoLancamento, ReadModelIndisponivel
from .observability import EVENTOS
from .service import ProjecaoSaldo

logger = logging.getLogger(__name__)
_log_seguranca = logging.getLogger("seguranca")

CABECALHO_ASSINATURA = "x-assinatura"
PREFIXO = "ed25519="


class Mensagem(Protocol):
    body: bytes
    message_id: str | None
    headers: dict

    async def ack(self) -> None: ...

    async def reject(self, requeue: bool = False) -> None: ...

    async def nack(self, requeue: bool = True) -> None: ...


def carregar_chaves_publicas(texto: str) -> list[Ed25519PublicKey]:
    """Lê "atual,anterior" (base64 dos 32 bytes de cada chave pública). Falha cedo se malformada."""
    chaves = []
    for parte in (p.strip() for p in texto.split(",")):
        if not parte:
            continue
        try:
            chaves.append(Ed25519PublicKey.from_public_bytes(base64.b64decode(parte, validate=True)))
        except (binascii.Error, ValueError) as exc:
            raise ValueError("chave pública Ed25519 inválida: esperado base64 de 32 bytes") from exc
    return chaves


def assinatura_valida(chaves: list[Ed25519PublicKey], corpo: bytes, cabecalho) -> bool:
    """Aceita qualquer chave da lista (rotação sem parada: atual + anterior)."""
    if isinstance(cabecalho, bytes):
        cabecalho = cabecalho.decode("ascii", errors="replace")
    if not isinstance(cabecalho, str) or not cabecalho.startswith(PREFIXO):
        return False
    try:
        assinatura = base64.b64decode(cabecalho[len(PREFIXO):], validate=True)
    except (binascii.Error, ValueError):
        return False
    for chave in chaves:
        try:
            chave.verify(assinatura, corpo)
            return True
        except InvalidSignature:
            continue
    return False


ESPERA_MAXIMA_S = 30.0


async def _aguardar(parar: asyncio.Event | None, espera_s: float) -> bool:
    """Espera `espera_s`. Devolve True se o encerramento do serviço foi pedido nesse intervalo."""
    if parar is None:
        await asyncio.sleep(espera_s)
        return False
    if parar.is_set():
        return True
    try:
        await asyncio.wait_for(parar.wait(), timeout=espera_s)
    except asyncio.TimeoutError:
        return False
    return True


async def tratar_mensagem(
    msg: Mensagem,
    projecao: ProjecaoSaldo,
    chaves: list[Ed25519PublicKey],
    backoff_s: float = 0.5,
    *,
    retencao_s: float = 1200.0,
    parar: asyncio.Event | None = None,
) -> str:
    """Processa uma mensagem e devolve o desfecho (útil para testes e métricas).

    `retencao_s`: por quanto tempo a mensagem fica retida aqui enquanto o banco está fora, antes
    de ser devolvida à fila (cada devolução conta uma entrega no `x-delivery-limit`). Precisa ficar
    abaixo do `consumer_timeout` do broker (30 min por padrão).
    """
    if not assinatura_valida(chaves, msg.body, (msg.headers or {}).get(CABECALHO_ASSINATURA)):
        EVENTOS.labels("assinatura_invalida").inc()
        _log_seguranca.error(
            "evento com assinatura inválida descartado",
            extra={"campos": {"evento": "assinatura_invalida", "message_id": msg.message_id}},
        )
        await msg.reject(requeue=False)
        return "dlq"
    try:
        evento = EventoLancamento.de_json(msg.body)
    except EventoInvalido as exc:
        EVENTOS.labels("invalido").inc()
        logger.error("evento inválido (message_id=%s) enviado para DLQ: %s", msg.message_id, exc)
        await msg.reject(requeue=False)
        return "dlq"
    inicio = time.monotonic()
    espera = backoff_s
    while True:
        try:
            await projecao.aplicar(evento)
            break
        except ReadModelIndisponivel as exc:
            # A mensagem está boa; quem falhou foi o banco. Segura e tenta de novo.
            EVENTOS.labels("banco_indisponivel").inc()
            retido_s = time.monotonic() - inicio
            esgotou = retido_s + espera >= retencao_s
            logger.warning(
                "banco do consolidado indisponível; evento %s retido há %.0fs: %s",
                evento.event_id, retido_s, exc,
            )
            if esgotou or await _aguardar(parar, espera):
                await msg.nack(requeue=True)
                return "requeue"
            espera = min(espera * 2, ESPERA_MAXIMA_S)
        except Exception as exc:
            EVENTOS.labels("erro").inc()
            logger.warning("erro inesperado no evento %s, devolvendo à fila: %s", evento.event_id, exc)
            await asyncio.sleep(backoff_s)
            await msg.nack(requeue=True)
            return "requeue"
    await msg.ack()
    return "ack"


class ConsumidorRabbit:
    def __init__(
        self,
        url: str,
        fila: str,
        prefetch: int,
        projecao: ProjecaoSaldo,
        chaves: list[Ed25519PublicKey],
        retencao_s: float = 1200.0,
    ):
        if not chaves:
            raise ValueError("ao menos uma chave pública Ed25519 é obrigatória")
        self._url = url
        self._fila = fila
        self._prefetch = prefetch
        self._projecao = projecao
        self._chaves = chaves
        self._retencao_s = retencao_s
        self._parar: asyncio.Event | None = None
        self._conexao = None

    async def _iniciar(self) -> None:
        self._conexao = await aio_pika.connect_robust(self._url, timeout=5)
        canal = await self._conexao.channel()
        await canal.set_qos(prefetch_count=self._prefetch)
        # Declaração passiva: só verifica que a fila existe (não exige permissão de configurar).
        fila = await canal.get_queue(self._fila, ensure=True)
        await fila.consume(self._ao_receber)
        logger.info("consumidor conectado à fila %s", self._fila)

    async def _ao_receber(self, msg: AbstractIncomingMessage) -> None:
        await tratar_mensagem(
            msg, self._projecao, self._chaves, retencao_s=self._retencao_s, parar=self._parar
        )

    async def executar(self, parar: asyncio.Event) -> None:
        """Conecta com retentativa. Broker fora do ar NÃO derruba a API de consulta."""
        self._parar = parar
        espera = 1.0
        while not parar.is_set():
            try:
                await self._iniciar()
                await parar.wait()
                return
            except Exception as exc:
                logger.warning("broker indisponível, nova tentativa em %.0fs: %s", espera, exc)
                await self.fechar()
                try:
                    await asyncio.wait_for(parar.wait(), timeout=espera)
                except asyncio.TimeoutError:
                    pass
                espera = min(espera * 2, 30)

    async def fechar(self) -> None:
        conexao, self._conexao = self._conexao, None
        if conexao is not None:
            try:
                await conexao.close()
            except Exception:  # pragma: no cover
                pass
