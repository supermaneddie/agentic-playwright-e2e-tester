import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    url: str
    model: str = Field(min_length=1)
    api_key: str | None = None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("llm.url must be an HTTP(S) base URL")
        return value.rstrip("/")


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8091, ge=1, le=65535)


class StorageConfig(BaseModel):
    directory: Path = Path("data")


class TestingConfig(BaseModel):
    request_timeout: float = Field(default=30, gt=0, le=300)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=10485760)
    max_steps: int = Field(default=30, ge=1, le=100)


class BrowserConfig(BaseModel):
    headless: bool = True
    action_timeout: float = Field(default=15, gt=0, le=120)
    step_timeout: float = Field(default=120, gt=0, le=600)
    screenshots: bool = True
    max_sessions: int = Field(default=4, ge=1, le=32)
    auto_selectors: bool = True
    vision: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm: LLMConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)


def load_config(path: str | Path | None = None) -> Settings:
    path = Path(path or os.getenv("CONFIG_FILE", "config.yml"))
    raw = yaml.safe_load(path.read_text())

    def expand(value):
        if isinstance(value, str):
            def replace(match):
                name = match.group(1)
                if name not in os.environ:
                    raise ValueError(f"Missing environment variable: {name}")
                return os.environ[name]
            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return Settings.model_validate(expand(raw))
