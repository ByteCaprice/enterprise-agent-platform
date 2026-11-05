"""Bootstrap configuration owned by the new platform processes.

Only process and infrastructure settings belong here. Capability, model, tool,
identity and delivery configuration are governed by their respective services.
"""

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from domain.platform_runtime import PlatformRuntimeSettings


class SettingsValidationError(ValueError):
    """The platform bootstrap environment is not safe to run."""


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    backend: Literal["postgresql"] = "postgresql"
    host: str | None = None
    port: int = Field(default=5432, ge=1, le=65535)
    name: str | None = None
    user: str | None = None
    password: str | None = Field(default=None, repr=False)
    schema_name: str | None = Field(default=None, alias="schema")
    sslmode: str = "prefer"
    sslrootcert: str | None = None
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    pool_timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    pool_max_size: int = Field(default=10, ge=1, le=1000)
    statement_timeout_ms: int | None = Field(default=None, ge=1, le=3600000)
    lock_timeout_ms: int | None = Field(default=None, ge=1, le=3600000)


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=50020, ge=1, le=65535)


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", case_sensitive=False, extra="ignore")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    platform_runtime: PlatformRuntimeSettings = Field(default_factory=PlatformRuntimeSettings)
    yaml_file: ClassVar[str | None] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = [init_settings, env_settings, dotenv_settings]
        if cls.yaml_file:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=cls.yaml_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def load_settings(config_dir: str | Path) -> dict:
    root = Path(config_dir)
    yaml_path = root / "settings.yaml"
    env_path = root / ".env"

    class RuntimeSettings(PlatformSettings):
        yaml_file: ClassVar[str | None] = str(yaml_path) if yaml_path.exists() else None

    return RuntimeSettings(
        _env_file=str(env_path) if env_path.exists() else None,
        _env_file_encoding="utf-8",
    ).model_dump(mode="python", by_alias=True)
