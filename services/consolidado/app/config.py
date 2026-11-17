from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração do serviço de Consolidado Diário (variáveis com prefixo CONS_)."""

    model_config = SettingsConfigDict(env_prefix="CONS_", extra="ignore")

    database_url: str = "postgresql://consolidado:consolidado@localhost:5432/consolidado"
    db_pool_min: int = 2
    db_pool_max: int = 20

    redis_url: str = "redis://localhost:6379/0"
    # Cache curto: o consolidado já é eventualmente consistente; o TTL limita a defasagem extra.
    cache_ttl_s: int = Field(default=5, ge=0, le=300)

    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    exchange: str = "fluxo-caixa.lancamentos"
    fila: str = "consolidado.lancamentos"
    prefetch: int = Field(default=50, gt=0, le=1000)
    consumidor_enabled: bool = True
    # Banco do read model fora: a mensagem fica retida no consumidor (retentativa com backoff) por
    # até este tempo antes de voltar à fila. Só a devolução conta no x-delivery-limit (10), então a
    # tolerância a uma queda do banco é ~10x este valor (padrão: ~3 h) em vez de minutos.
    # Manter abaixo do consumer_timeout do RabbitMQ (30 min por padrão).
    retencao_indisponibilidade_s: float = Field(default=1200, ge=0, le=1500)
    # A topologia (fila quorum, x-delivery-limit, DLQ) é criada pela infraestrutura
    # (infra/rabbitmq/topologia.sh): o usuário deste serviço só tem permissão de consumir.

    # Chaves PÚBLICAS Ed25519 (base64 dos 32 bytes) que conferem a assinatura dos eventos,
    # separadas por vírgula ("atual,anterior") para rotação sem parada. Obrigatória com o
    # consumidor ligado. Este serviço nunca recebe a chave privada: não consegue assinar.
    eventos_chaves_publicas: str | None = None

    # Swagger/OpenAPI desligado por padrão; só o ambiente local liga.
    docs_habilitado: bool = False

    # Só a chave PÚBLICA do IdP (confere tokens, não emite). Local: Ed25519 (base64 dos 32 bytes);
    # produção: JWKS do IdP com RS256. Algoritmo simétrico (HS*) é recusado na subida.
    jwt_chave_publica: str | None = None
    jwt_jwks_url: str | None = None
    jwt_algorithm: str = "EdDSA"
    jwt_audience: str = "fluxo-caixa"
    jwt_issuer: str = "fluxo-caixa-idp-local"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
