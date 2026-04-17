from __future__ import annotations

from pathlib import Path

from hope.core.config import MemoryFilesConfig, SystemPromptConfig


def test_base_agent_uses_builder(tmp_path: Path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("I am Hope.")
    memory = tmp_path / "MEMORY.md"
    memory.write_text("- User likes Python")

    from hope.prompt.builder import SystemPromptBuilder

    builder = SystemPromptBuilder(
        agent_template="You are a helpful assistant.",
        memory_files_config=MemoryFilesConfig(
            soul_path=str(soul),
            memory_path=str(memory),
            user_path=str(tmp_path / "USER.md"),
        ),
        system_prompt_config=SystemPromptConfig(),
    )
    prompt = builder.build()
    assert "Hope" in prompt
    assert "Python" in prompt
    assert "helpful assistant" in prompt
