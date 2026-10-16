"""Registered execution contracts; a model definition cannot install a provider."""

from dataclasses import dataclass
from typing import Literal

Task = Literal["llm", "embedding", "ocr"]


@dataclass(frozen=True)
class AdapterDefinition:
    task: Task
    provider: str
    execution: Literal["remote", "local", "stub"]
    reasoning: bool = True
    tools: bool = True
    streaming: bool = True
    structured_output: bool = True
    prompt_cache: bool = True


ADAPTERS: dict[str, AdapterDefinition] = {
    "openai-responses": AdapterDefinition("llm", "openai", "remote"),
    "stub": AdapterDefinition("llm", "stub", "stub"),
    "ollama-native": AdapterDefinition(
        "llm", "ollama", "local", reasoning=False, tools=False, prompt_cache=False
    ),
}
