"""Modelo do contexto de Consolidado Diário (read model / projeção).

O Consolidado não é dono dos lançamentos: ele consome o evento publicado por Lançamentos
(linguagem publicada, v1) e mantém o saldo por comerciante e dia. A projeção é:
- comutativa: a ordem de chegada dos eventos não altera o resultado;
- idempotente: o mesmo event_id aplicado duas vezes conta uma vez só.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

ZERO = Decimal("0.00")


class EventoInvalido(Exception):
    """Mensagem fora do contrato. Não adianta reprocessar: vai direto para a DLQ."""


class ReadModelIndisponivel(Exception):
    """O banco do read model não respondeu. A culpa não é da mensagem: o consumidor a segura e
    tenta de novo, sem devolvê-la à fila (não consome o `x-delivery-limit`)."""


@dataclass(frozen=True)
class EventoLancamento:
    event_id: UUID
    comerciante_id: str
    lancamento_id: UUID
    tipo: str
    valor: Decimal
    data_competencia: date
    occurred_at: datetime

    @classmethod
    def de_json(cls, corpo: bytes) -> "EventoLancamento":
        try:
            d = json.loads(corpo)
            if d.get("event_type") != "LancamentoRegistrado" or d.get("event_version") != 1:
                raise EventoInvalido(f"tipo/versão não suportados: {d.get('event_type')} v{d.get('event_version')}")
            tipo = d["tipo"]
            if tipo not in ("CREDITO", "DEBITO"):
                raise EventoInvalido(f"tipo de lançamento inválido: {tipo}")
            valor = Decimal(d["valor"])
            if valor <= 0 or valor != valor.quantize(Decimal("0.01")):
                raise EventoInvalido(f"valor inválido: {valor}")
            comerciante = d["comerciante_id"]
            if not isinstance(comerciante, str) or not comerciante:
                raise EventoInvalido("comerciante_id ausente")
            return cls(
                event_id=UUID(d["event_id"]),
                comerciante_id=comerciante,
                lancamento_id=UUID(d["lancamento_id"]),
                tipo=tipo,
                valor=valor,
                data_competencia=date.fromisoformat(d["data_competencia"]),
                occurred_at=datetime.fromisoformat(d["occurred_at"]),
            )
        except EventoInvalido:
            raise
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise EventoInvalido(f"payload fora do contrato: {exc}") from exc

    @property
    def credito(self) -> Decimal:
        return self.valor if self.tipo == "CREDITO" else ZERO

    @property
    def debito(self) -> Decimal:
        return self.valor if self.tipo == "DEBITO" else ZERO


@dataclass(frozen=True)
class RelatorioDiario:
    data: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_dia: Decimal
    saldo_acumulado: Decimal
    quantidade_lancamentos: int
    atualizado_em: datetime | None

    @classmethod
    def calcular(
        cls,
        *,
        data: date,
        creditos: Decimal,
        debitos: Decimal,
        quantidade: int,
        saldo_acumulado: Decimal,
        atualizado_em: datetime | None,
    ) -> "RelatorioDiario":
        return cls(
            data=data,
            total_creditos=creditos,
            total_debitos=debitos,
            saldo_dia=creditos - debitos,
            saldo_acumulado=saldo_acumulado,
            quantidade_lancamentos=quantidade,
            atualizado_em=atualizado_em,
        )

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), default=str).encode()

    @classmethod
    def de_json(cls, corpo: bytes) -> "RelatorioDiario":
        d = json.loads(corpo)
        return cls(
            data=date.fromisoformat(d["data"]),
            total_creditos=Decimal(d["total_creditos"]),
            total_debitos=Decimal(d["total_debitos"]),
            saldo_dia=Decimal(d["saldo_dia"]),
            saldo_acumulado=Decimal(d["saldo_acumulado"]),
            quantidade_lancamentos=int(d["quantidade_lancamentos"]),
            atualizado_em=datetime.fromisoformat(d["atualizado_em"]) if d["atualizado_em"] else None,
        )
