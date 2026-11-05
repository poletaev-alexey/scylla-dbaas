from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DBAAS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./dbaas.db"
    provider: Literal["fake", "local", "aws"] = "fake"

    # API keys: "key1:alice,key2:bob" -> key -> Actor name.
    api_keys: str = "dev-key:dev"

    # Health checking / Auto-recovery
    health_interval_seconds: float = 15.0
    recovery_grace_seconds: float = 120.0

    # Backups
    default_backup_interval_hours: int = 24
    default_backup_retention: int = 7

    # AWS driver
    aws_region: str = "eu-central-1"
    operator_cidr: str = "0.0.0.0/32"
    terraform_dir: str = "../infra/cluster"
    terraform_state_bucket: str = ""
    backup_bucket: str = ""

    def actor_for_key(self, key: str) -> str | None:
        for pair in self.api_keys.split(","):
            k, _, actor = pair.strip().partition(":")
            if k and k == key:
                return actor or k
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
