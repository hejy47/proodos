from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIRNAME = "output"

LLMProvider = Literal["openai", "deepseek"]


@dataclass(frozen=True)
class LLMSettings:
    provider: LLMProvider
    api_key: str | None
    model: str
    base_url: str
    thinking: bool | None = None
    max_tokens: int = 16384
    request_timeout: float = 120
    temperature: float = 0.0


@dataclass(frozen=True)
class PathSettings:
    project_root: Path
    project_path: Path
    output_dir: Path


@dataclass(frozen=True)
class RuntimeSettings:
    llm: LLMSettings
    preprocess_paths: PathSettings | None
    localization_paths: PathSettings | None

    @property
    def paths(self) -> PathSettings:
        if self.preprocess_paths is not None:
            return self.preprocess_paths
        if self.localization_paths is not None:
            return self.localization_paths
        raise AttributeError("RuntimeSettings has no configured path settings")


def load_dotenv_file(dotenv_path: Path | None = None) -> None:
    """Populate unset environment variables from a local .env file."""
    dotenv_path = dotenv_path or PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def resolve_project_path(project_path: str | Path | None) -> Path:
    if project_path is None:
        return PROJECT_ROOT

    candidate = Path(project_path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    return candidate


def resolve_output_dir(output_dir: str | Path | None) -> Path:
    if output_dir is None:
        return PROJECT_ROOT / DEFAULT_OUTPUT_DIRNAME

    candidate = Path(output_dir).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    return candidate


def _build_path_settings(
    *,
    project_root: Path,
    project_path: Path,
    output_dir: Path,
) -> PathSettings:
    paths = PathSettings(
        project_root=project_root,
        project_path=project_path,
        output_dir=output_dir,
    )
    return paths


def load_llm_settings() -> LLMSettings:
    """Load LLM config from ``LLM_PROVIDER`` and provider-specific env vars."""
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "deepseek":
        return LLMSettings(
            provider="deepseek",
            api_key=_first_nonempty_env("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            thinking=_thinking_mode(),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "16384")),
            request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "120")),
        )
    return LLMSettings(
        provider="openai",
        api_key=os.getenv("OPENAI_API_KEY"),
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "16384")),
        request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "120")),
    )


def _thinking_mode() -> bool | None:
    value = os.getenv("DEEPSEEK_THINKING", "").strip().lower()
    if not value:
        return None
    if value not in {"enabled", "disabled"}:
        raise ValueError("DEEPSEEK_THINKING must be enabled or disabled")
    return value == "enabled"


def _first_nonempty_env(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return None


def build_runtime_settings(
    project_path: str | Path | None = None,
    preprocess_dir: str | Path | None = None,
    localization_dir: str | Path | None = None,
) -> RuntimeSettings:
    load_dotenv_file()

    resolved_project_path = resolve_project_path(project_path)
    preprocess_paths = (
        _build_path_settings(
            project_root=PROJECT_ROOT,
            project_path=resolved_project_path,
            output_dir=resolve_output_dir(preprocess_dir),
        )
        if preprocess_dir is not None
        else None
    )
    localization_paths = (
        _build_path_settings(
            project_root=PROJECT_ROOT,
            project_path=resolved_project_path,
            output_dir=resolve_output_dir(localization_dir),
        )
        if localization_dir is not None
        else None
    )

    llm = load_llm_settings()
    return RuntimeSettings(
        llm=llm,
        preprocess_paths=preprocess_paths,
        localization_paths=localization_paths,
    )
