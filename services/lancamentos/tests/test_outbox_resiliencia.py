"""Requisito não funcional central: Lançamentos continua aceitando registros
mesmo com broker/Consolidado fora do ar, e nada se perde quando eles voltam."""

import asyncio
import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from prometheus_client import REGISTRY

from app.assinatura import assinar, carregar_chave_privada, gerar_par
from app.observability import OUTBOX_PENDENTES
from app.outbox import FalhaPublicacao, RabbitPublicador, executar_relay, publicar_pendentes
from conftest import cabecalhos


def _verificar(publica_b64: str, cabecalho: str, corpo: bytes) -> None:
    publica = Ed25519PublicKey.from_public_bytes(base64.b64decode(publica_b64))
    publica.verify(base64.b64decode(cabecalho.removeprefix("ed25519=")), corpo)


def test_evento_assinado_com_ed25519_e_conferido_so_com_a_chave_publica():
    privada_b64, publica_b64 = gerar_par()
    corpo = b'{"event_id":"1"}'
    cabecalho = assinar(carregar_chave_privada(privada_b64), corpo)
    assert cabecalho.startswith("ed25519=")
    _verificar(publica_b64, cabecalho, corpo)  # não lança: assinatura válida
    with pytest.raises(InvalidSignature):
        _verificar(publica_b64, cabecalho, corpo + b" ")  # qualquer alteração no corpo invalida
    with pytest.raises(InvalidSignature):
        _verificar(gerar_par()[1], cabecalho, corpo)  # outra chave pública não confere


@pytest.mark.parametrize("texto", ["", "nao-e-base64!!", base64.b64encode(b"curta").decode()])
def test_chave_privada_malformada_e_recusada(texto):
    with pytest.raises(ValueError):
        carregar_chave_privada(texto)


def test_publicador_exige_chave_privada():
    with pytest.raises(ValueError):
        RabbitPublicador("amqp://localhost", "ex", None)


def test_nao_ha_mais_variavel_de_chave_compartilhada():
    from app.config import Settings

    assert "eventos_hmac_chave" not in Settings.model_fields
    assert "eventos_chave_privada" in Settings.model_fields


class PublicadorFake:
    def __init__(self, falhar_apos: int | None = None):
        self.enviados: list[tuple[str, bytes, str]] = []
        self.falhar_apos = falhar_apos  # None = nunca falha; 0 = broker fora do ar

    async def publicar(self, routing_key, corpo, message_id):
        if self.falhar_apos is not None and len(self.enviados) >= self.falhar_apos:
            raise ConnectionError("broker indisponível")
        self.enviados.append((routing_key, corpo, message_id))

    async def fechar(self):
        pass


def _registrar(client, n: int):
    for i in range(n):
        r = client.post("/v1/lancamentos", json={"tipo": "CREDITO", "valor": "1.00"}, headers=cabecalhos(f"chave-res-{i:04d}"))
        assert r.status_code == 201


async def test_registro_funciona_com_broker_fora_e_eventos_ficam_pendentes(client, repo):
    _registrar(client, 3)  # API não depende do broker
    with pytest.raises(FalhaPublicacao):
        await publicar_pendentes(repo, PublicadorFake(falhar_apos=0), limite=10)
    assert await repo.contar_pendentes() == 3


async def test_falha_no_meio_do_lote_confirma_so_o_que_foi_publicado(client, repo):
    _registrar(client, 5)
    with pytest.raises(FalhaPublicacao):
        await publicar_pendentes(repo, PublicadorFake(falhar_apos=2), limite=10)
    assert await repo.contar_pendentes() == 3

    pub = PublicadorFake()
    assert await publicar_pendentes(repo, pub, limite=10) == 3
    assert await repo.contar_pendentes() == 0
    assert all(rk == "lancamento.registrado.v1" for rk, _, _ in pub.enviados)


async def test_relay_drena_backlog_quando_broker_volta(client, repo):
    _registrar(client, 4)
    pub = PublicadorFake()
    parar = asyncio.Event()
    tarefa = asyncio.create_task(executar_relay(repo, pub, limite=2, intervalo_s=0.01, parar=parar))
    for _ in range(100):
        if await repo.contar_pendentes() == 0:
            break
        await asyncio.sleep(0.01)
    parar.set()
    await asyncio.wait_for(tarefa, timeout=1)
    assert len(pub.enviados) == 4
    assert len({mid for _, _, mid in pub.enviados}) == 4  # message_id = event_id (dedupe no consumidor)


async def test_metrica_de_backlog_cresce_com_broker_fora(client, repo):
    # Sem isso o alerta OutboxAcumulando nunca dispararia: é justamente com o broker fora
    # que o gauge precisa refletir os eventos parados.
    OUTBOX_PENDENTES.set(0)
    _registrar(client, 3)
    parar = asyncio.Event()
    tarefa = asyncio.create_task(
        executar_relay(repo, PublicadorFake(falhar_apos=0), limite=10, intervalo_s=0.01, parar=parar)
    )
    for _ in range(100):
        if _backlog() == 3:
            break
        await asyncio.sleep(0.01)
    parar.set()
    await asyncio.wait_for(tarefa, timeout=1)
    assert _backlog() == 3


def _backlog() -> float | None:
    return REGISTRY.get_sample_value("outbox_eventos_pendentes")
