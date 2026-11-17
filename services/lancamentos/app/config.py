from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração do serviço de Lançamentos (variáveis com prefixo LANC_)."""

    model_config = SettingsConfigDict(env_prefix="LANC_", extra="ignore")

    database_url: str = "postgresql://lancamentos:lancamentos@localhost:5432/lancamentos"
    db_pool_min: int = 2
    db_pool_max: int = 10

    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    exchange: str = "fluxo-caixa.lancamentos"

    # Autenticação: o serviço recebe só a chave PÚBLICA do IdP (confere tokens, não emite).
    # Local: Ed25519 do IdP local (base64 dos 32 bytes). Produção: JWKS do IdP com RS256.
    # Algoritmo simétrico (HS*) é recusado na subida.
    jwt_chave_publica: str | None = None
    jwt_jwks_url: str | None = None
    jwt_algorithm: str = "EdDSA"
    jwt_audience: str = "fluxo-caixa"
    jwt_issuer: str = "fluxo-caixa-idp-local"

    # Chave PRIVADA Ed25519 (base64 dos 32 bytes) que assina cada evento publicado.
    # Só este serviço a recebe; o Consolidado tem apenas a pública. Obrigatória com o relay ligado.
    # Em produção: Key Vault. Gerar: `python -m app.gerar_chaves` (ou `make chaves`).
    eventos_chave_privada: str | None = None

    # Swagger/OpenAPI desligado por padrão; só o ambiente local liga.
    docs_habilitado: bool = False

    outbox_relay_enabled: bool = True
    outbox_poll_interval_s: float = Field(default=0.5, gt=0)
    outbox_batch_size: int = Field(default=100, gt=0, le=1000)

    # Fuso usado para definir "hoje" do comerciante (data de competência padrão).
    timezone: str = "America/Sao_Paulo"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
