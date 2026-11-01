"""Settings, read from ``AGENT_*`` environment variables (and ``.env``).

The Claude credentials are not among them: the Anthropic SDK reads
``ANTHROPIC_API_KEY`` (or an ``ant auth login`` profile) by itself.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from almena_agent.metrics import parse_addr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production", "test"] = "development"
    host: str = "127.0.0.1"
    port: int = 8100
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Proxies whose X-Forwarded-* headers are trusted (comma-separated IPs or "*").
    forwarded_allow_ips: str = "127.0.0.1"

    # Public origin of the agent: the agent card advertises its A2A endpoints there.
    public_url: str = "https://agent.almena.id"
    # How the agent card names the agent and its provider.
    name: str = "Almena Agent"
    description: str = "The AI agent of Almena ID."
    provider_organization: str = "Almena ID"
    provider_url: str = "https://almena.id"

    # Claude: the model, how hard it thinks, and the most it writes per reply.
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = Field(default=64000, gt=0)
    # What the agent is told about itself; empty uses prompts.SYSTEM_PROMPT.
    system_prompt: str = ""
    # Earlier turns of a conversation (A2A context) kept and sent back to Claude.
    history_turns: int = Field(default=50, gt=0)

    # The registry API's MCP endpoint: its tools, used with each caller's own
    # registry token (empty: no tools; a token is still required).
    registry_mcp_url: str = "https://api.almena.id/mcp"
    # Rounds of tool calls one reply may take before it must answer.
    max_tool_rounds: int = Field(default=20, gt=0)

    # The registry API's signed-in account: every A2A call's token is checked
    # there, and its account is the caller. Required: without it nobody gets in.
    registry_account_url: str = Field(default="https://api.almena.id/api/v1/auth/me", min_length=1)
    # How long an accepted token is trusted before the API is asked again.
    token_cache_seconds: int = Field(default=60, ge=0)
    # Per window: A2A calls per account, and refused tokens per client IP.
    rate_limit_window_seconds: int = Field(default=60, gt=0)
    rate_limit: int = Field(default=30, gt=0)
    auth_failure_limit: int = Field(default=10, gt=0)
    # Conversations kept in memory: dropped once idle this long, and the least
    # recently used beyond this many.
    conversation_idle_minutes: int = Field(default=60, gt=0)
    max_conversations: int = Field(default=1000, gt=0)
    # Finished tasks kept in memory: dropped once idle this long, and the least
    # recently used beyond this many tasks in all (in-flight ones always stay).
    task_idle_minutes: int = Field(default=60, gt=0)
    max_tasks: int = Field(default=5000, gt=0)
    # Prometheus metrics on their own listener (host:port, e.g. 127.0.0.1:9090),
    # never on the public app; empty: off. Keep it off the public proxy.
    metrics_addr: str = ""

    @field_validator("metrics_addr")
    @classmethod
    def _metrics_addr(cls, value: str) -> str:
        value = value.strip()
        if value:
            parse_addr(value)
        return value

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
