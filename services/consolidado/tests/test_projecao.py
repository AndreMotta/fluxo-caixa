"""Regras da projeção do saldo diário e do consumo de mensagens."""

import base64
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.consumer import assinatura_valida, carregar_chaves_publicas, tratar_mensagem
from app.domain import EventoInvalido, EventoLancamento
from app.service import ProjecaoSaldo
from conftest import evento, evento_json


async def test_saldo_do_dia_e_acumulado(repo):
    p = ProjecaoSaldo(repo)
    await p.aplicar(evento(tipo="CREDITO", valor="100.00", data="2026-11-09"))
    await p.aplicar(evento(tipo="DEBITO", valor="30.00", data="2026-11-09"))
    await p.aplicar(evento(tipo="CREDITO", valor="50.25", data="2026-11-10"))
    await p.aplicar(evento(tipo="DEBITO", valor="80.00", data="2026-11-10"))

    rel = await repo.relatorio("loja-1", date(2026, 11, 10))
    assert rel.total_creditos == Decimal("50.25")
    assert rel.total_debitos == Decimal("80.00")
    assert rel.saldo_dia == Decimal("-29.75")
    assert rel.saldo_acumulado == Decimal("40.25")  # 70.00 de ontem + (-29.75) de hoje
    assert rel.quantidade_lancamentos == 2


async def test_mesmo_evento_entregue_duas_vezes_conta_uma(repo):
    p = ProjecaoSaldo(repo)
    eid = uuid4()
    assert await p.aplicar(evento(valor="10.00", event_id=eid)) is True
    assert await p.aplicar(evento(valor="10.00", event_id=eid)) is False
    rel = await repo.relatorio("loja-1", date(2026, 11, 10))
    assert rel.total_creditos == Decimal("10.00") and rel.quantidade_lancamentos == 1


async def test_ordem_de_chegada_nao_altera_resultado(repo):
    from conftest import RepositorioEmMemoria

    eventos = [evento(tipo=t, valor=v) for t, v in [("CREDITO", "5.00"), ("DEBITO", "2.50"), ("CREDITO", "1.10")]]
    outro = RepositorioEmMemoria()
    for e in eventos:
        await ProjecaoSaldo(repo).aplicar(e)
    for e in reversed(eventos):
        await ProjecaoSaldo(outro).aplicar(e)
    d = date(2026, 11, 10)
    assert await repo.relatorio("loja-1", d) == await outro.relatorio("loja-1", d)


async def test_comerciantes_isolados(repo):
    p = ProjecaoSaldo(repo)
    await p.aplicar(evento(valor="10.00", comerciante="loja-1"))
    await p.aplicar(evento(valor="99.00", comerciante="loja-2"))
    assert (await repo.relatorio("loja-1", date(2026, 11, 10))).saldo_dia == Decimal("10.00")


@pytest.mark.parametrize(
    "corpo",
    [
        b"nao-e-json",
        evento_json(tipo="PIX"),
        evento_json(valor="-1.00"),
        evento_json(valor="1.234"),
        evento_json(event_version=2),
        evento_json(comerciante=""),
    ],
)
def test_contrato_do_evento_e_validado(corpo):
    with pytest.raises(EventoInvalido):
        EventoLancamento.de_json(corpo)


# Nos testes a chave privada existe só para simular o que o serviço de Lançamentos faz.
# O Consolidado em si recebe apenas as chaves públicas (CHAVES).
PRIVADA = Ed25519PrivateKey.generate()
CHAVES = [PRIVADA.public_key()]


def _assinar(corpo: bytes, chave: Ed25519PrivateKey = PRIVADA) -> str:
    return "ed25519=" + base64.b64encode(chave.sign(corpo)).decode()


def _publica_b64(privada: Ed25519PrivateKey) -> str:
    bruto = privada.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(bruto).decode()


class MensagemFake:
    def __init__(self, body: bytes, assinatura: str | None = "auto"):
        self.body = body
        self.message_id = "m-1"
        self.headers = {} if assinatura is None else {"x-assinatura": _assinar(body) if assinatura == "auto" else assinatura}
        self.desfecho = None

    async def ack(self):
        self.desfecho = "ack"

    async def reject(self, requeue=False):
        self.desfecho = f"reject(requeue={requeue})"

    async def nack(self, requeue=True):
        self.desfecho = f"nack(requeue={requeue})"


async def test_mensagem_valida_recebe_ack(repo):
    msg = MensagemFake(evento_json())
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0) == "ack"
    assert msg.desfecho == "ack"


async def test_mensagem_fora_do_contrato_vai_para_dlq_sem_retentar(repo):
    msg = MensagemFake(b"{}")
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0) == "dlq"
    assert msg.desfecho == "reject(requeue=False)"


async def test_banco_fora_retem_a_mensagem_e_aplica_quando_ele_volta(repo):
    # Queda do banco não devolve a mensagem à fila: nenhuma entrega é gasta do x-delivery-limit.
    repo.falhas_restantes = 25  # bem mais que o x-delivery-limit (10)
    msg = MensagemFake(evento_json(valor="10.00"))
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0) == "ack"
    assert msg.desfecho == "ack"
    assert repo.tentativas == 26
    assert (await repo.relatorio("loja-1", date(2026, 11, 10))).total_creditos == Decimal("10.00")


async def test_banco_fora_alem_do_tempo_de_retencao_devolve_para_fila(repo):
    repo.falhar = True
    msg = MensagemFake(evento_json())
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0, retencao_s=0) == "requeue"
    assert msg.desfecho == "nack(requeue=True)"


async def test_encerramento_do_servico_devolve_a_mensagem_retida(repo):
    import asyncio

    repo.falhar = True
    parar = asyncio.Event()
    parar.set()
    msg = MensagemFake(evento_json())
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0, parar=parar) == "requeue"
    assert msg.desfecho == "nack(requeue=True)"
    assert repo.tentativas == 1


async def test_erro_inesperado_devolve_para_fila_sem_reter(repo):
    # Não é indisponibilidade do banco: volta à fila e o x-delivery-limit decide (DLQ se persistir).
    repo.defeito = True
    msg = MensagemFake(evento_json())
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0) == "requeue"
    assert msg.desfecho == "nack(requeue=True)"
    assert repo.tentativas == 1


@pytest.mark.parametrize(
    "assinatura",
    [
        None,  # sem cabeçalho
        "ed25519=" + base64.b64encode(bytes(64)).decode(),  # assinatura forjada
        _assinar(b"outro corpo"),  # assinatura de outra mensagem (replay de cabeçalho)
        "ed25519=nao-e-base64!!",
        "v1=" + "0" * 64,  # formato HMAC antigo: não é mais aceito
        "sha256=abc",  # formato desconhecido
    ],
)
async def test_evento_sem_assinatura_valida_nao_altera_saldo(repo, assinatura):
    msg = MensagemFake(evento_json(valor="1000000.00"), assinatura=assinatura)
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0) == "dlq"
    assert msg.desfecho == "reject(requeue=False)"
    assert (await repo.relatorio("loja-1", date(2026, 11, 10))).total_creditos == Decimal("0.00")


async def test_evento_assinado_por_outra_chave_privada_nao_altera_saldo(repo):
    # Mesmo corpo, assinatura válida em si, mas feita por quem não é o serviço de Lançamentos.
    corpo = evento_json(valor="1000000.00")
    msg = MensagemFake(corpo, assinatura=_assinar(corpo, chave=Ed25519PrivateKey.generate()))
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), CHAVES, backoff_s=0) == "dlq"
    assert (await repo.relatorio("loja-1", date(2026, 11, 10))).total_creditos == Decimal("0.00")


async def test_rotacao_aceita_chave_anterior(repo):
    atual, anterior = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chaves = carregar_chaves_publicas(f"{_publica_b64(atual)}, {_publica_b64(anterior)}")
    corpo = evento_json()
    msg = MensagemFake(corpo, assinatura=_assinar(corpo, chave=anterior))
    assert await tratar_mensagem(msg, ProjecaoSaldo(repo), chaves, backoff_s=0) == "ack"


def test_assinatura_em_bytes_tambem_e_aceita():
    corpo = b"{}"
    assert assinatura_valida(CHAVES, corpo, _assinar(corpo).encode())


def test_consolidado_so_tem_chave_publica_e_nao_consegue_assinar():
    chaves = carregar_chaves_publicas(_publica_b64(PRIVADA))
    assert len(chaves) == 1
    assert not hasattr(chaves[0], "sign")  # a chave pública só confere; não produz assinatura


@pytest.mark.parametrize("texto", ["nao-e-base64!!", base64.b64encode(b"curta").decode()])
def test_chave_publica_malformada_e_recusada(texto):
    with pytest.raises(ValueError):
        carregar_chaves_publicas(texto)
