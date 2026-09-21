"""
Configuration for the CUA system.

All settings are configurable via environment variables or CLI flags.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


class LLMConfig(BaseModel):
    """OpenAI-compatible LLM endpoint configuration."""

    base_url: str = Field(
        default_factory=lambda: os.getenv("CUA_LLM_BASE_URL", "https://api.openai.com/v1")
    )
    api_key: str = Field(
        default_factory=lambda: os.getenv("CUA_LLM_API_KEY", "")
    )
    model: str = Field(
        default_factory=lambda: os.getenv("CUA_LLM_MODEL", "gpt-4o")
    )
    max_tokens: int = 4096
    temperature: float = 0.0


class BrowserConfig(BaseModel):
    """Playwright browser settings."""

    headless: bool = Field(
        default_factory=lambda: os.getenv("CUA_BROWSER_HEADLESS", "true").lower() == "true"
    )
    viewport_width: int = 1280
    viewport_height: int = 720
    default_timeout_ms: int = 10000
    cdp_port: int = Field(
        default_factory=lambda: int(os.getenv("CUA_CDP_PORT", "9222"))
    )
    screenshot_dir: str = Field(
        default_factory=lambda: os.getenv("CUA_SCREENSHOT_DIR", "evidence/screenshots")
    )


class EscalationConfig(BaseModel):
    """Human escalation settings."""

    operator_host: str = "127.0.0.1"
    operator_port: int = Field(
        default_factory=lambda: int(os.getenv("CUA_OPERATOR_PORT", "8080"))
    )
    escalation_timeout_seconds: int = 300  # how long to wait for human before aborting


class Config(BaseModel):
    """Top-level application configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    artifact_dir: str = Field(
        default_factory=lambda: os.getenv("CUA_ARTIFACT_DIR", "evidence/artifacts")
    )
    log_dir: str = Field(
        default_factory=lambda: os.getenv("CUA_LOG_DIR", "evidence/logs")
    )
