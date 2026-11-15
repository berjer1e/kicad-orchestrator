"""Delegate isolated KiCad reasoning, BOM parsing, and code drafting tasks to Gemini API or Google Antigravity SDK."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any


TIER_MODELS: dict[str, str] = {
    "trivial": "gemini-3.5-flash-lite",
    "simple": "gemini-3.8-flash",
    "complex": "gemini-4-argon",
    "heavy": "gemini-4-carbon",
}


def resolve_model(task_tier: str = "simple", model: str | None = None) -> str:
    """Resolve the concrete model identifier from an explicit override or task tier.

    Args:
        task_tier: Workload tier (`trivial`, `simple`, `complex`, or `heavy`).
        model: Optional explicit model name overriding `task_tier`.

    Returns:
        Model identifier string accepted by `google-genai` / `google-antigravity`.
    """
    if model:
        return model.strip()
    return TIER_MODELS.get(task_tier.strip().lower(), TIER_MODELS["simple"])


def _read_optional_text(inline_text: str | None, file_path: str | None) -> str | None:
    """Load text from an inline string or UTF-8 file path when provided.

    Args:
        inline_text: Direct text string, if supplied.
        file_path: Path to a UTF-8 text file, if supplied.

    Returns:
        Combined stripped text string, or `None` if neither source is provided.
    """
    parts: list[str] = []
    if file_path:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        parts.append(path.read_text(encoding="utf-8"))
    if inline_text:
        parts.append(inline_text)
    if not parts:
        return None
    return "\n\n".join(parts).strip()


def _build_full_input(prompt: str, context_files: list[str] | None = None) -> str:
    """Prepend disk context files to the user prompt using tagged sections.

    Args:
        prompt: Core task prompt text.
        context_files: Optional list of file paths (e.g. `state_summary.json`, `SKILL.md`).

    Returns:
        Combined input prompt containing attached disk context followed by the task prompt.
    """
    sections: list[str] = []
    for raw_path in context_files or []:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Context file not found: {path}")
        content = path.read_text(encoding="utf-8")
        sections.append(f"=== CONTEXT FILE: {path.name} ({path}) ===\n{content}")
    sections.append(f"=== TASK ===\n{prompt}")
    return "\n\n".join(sections)


def _extract_interaction_text(interaction: Any) -> str:
    """Extract readable output text from a Gemini Interactions response object.

    Args:
        interaction: Response object returned by `client.interactions.create(...)`.

    Returns:
        Best-effort extracted text string.
    """
    text = getattr(interaction, "output_text", None)
    if isinstance(text, str) and text:
        return text
    outputs = getattr(interaction, "outputs", None)
    if isinstance(outputs, list):
        chunks: list[str] = []
        for item in outputs:
            item_text = getattr(item, "text", None)
            if isinstance(item_text, str) and item_text:
                chunks.append(item_text)
        if chunks:
            return "\n".join(chunks)
    return ""


def _run_genai_interaction(
    prompt: str,
    model: str,
    system_instruction: str | None = None,
    previous_interaction_id: str | None = None,
    max_output_tokens: int | None = None,
    background: bool = False,
) -> dict[str, Any]:
    """Execute a stateless or chained request through `google-genai` Interactions API.

    Args:
        prompt: Fully assembled task input text.
        model: Resolved Gemini model name.
        system_instruction: Optional system role or skill prompt.
        previous_interaction_id: Optional prior interaction ID for multi-turn state.
        max_output_tokens: Optional token cap passed via `generation_config`.
        background: Whether to request server-side background execution.

    Returns:
        Structured dictionary with `status`, `backend`, `model`, `interaction_id`,
        `interaction_status`, and `output_text`.
    """
    from google import genai

    client = genai.Client()
    kwargs: dict[str, Any] = {
        "model": model,
        "input": prompt,
    }
    if system_instruction:
        kwargs["system_instruction"] = system_instruction
    if previous_interaction_id:
        kwargs["previous_interaction_id"] = previous_interaction_id
    if max_output_tokens is not None:
        kwargs["generation_config"] = {"max_output_tokens": int(max_output_tokens)}
    if background:
        kwargs["background"] = True

    interaction = client.interactions.create(**kwargs)
    return {
        "status": "ok",
        "backend": "genai",
        "model": model,
        "interaction_id": getattr(interaction, "id", None),
        "interaction_status": getattr(interaction, "status", None),
        "output_text": _extract_interaction_text(interaction),
    }


async def _run_antigravity_async(
    prompt: str,
    model: str,
    system_instruction: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Execute a local managed agent turn through `google-antigravity` SDK.

    Args:
        prompt: Fully assembled task input text.
        model: Resolved Gemini model name.
        system_instruction: Optional system instructions passed to `LocalAgentConfig`.
        workspace: Optional workspace directory path exposed to the local agent.

    Returns:
        Structured dictionary with `status`, `backend`, `model`, and `output_text`.
    """
    from google.antigravity import Agent, LocalAgentConfig

    config_kwargs: dict[str, Any] = {"model": model}
    if system_instruction:
        config_kwargs["system_instructions"] = system_instruction
    if workspace:
        config_kwargs["workspaces"] = [str(Path(workspace).expanduser().resolve())]

    config = LocalAgentConfig(**config_kwargs)
    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        output_text = await response.text()

    return {
        "status": "ok",
        "backend": "antigravity",
        "model": model,
        "output_text": output_text,
    }


def delegate_task(
    prompt: str,
    task_tier: str = "simple",
    model: str | None = None,
    system_instruction: str | None = None,
    context_files: list[str] | None = None,
    backend: str = "genai",
    previous_interaction_id: str | None = None,
    max_output_tokens: int | None = None,
    background: bool = False,
    workspace: str | None = None,
    output_file: str | None = None,
) -> dict[str, Any]:
    """Route a delegated KiCad/code/spec task to Google Gemini or Antigravity SDK APIs.

    Args:
        prompt: Task instructions for the delegated model or agent.
        task_tier: Routing tier (`trivial`, `simple`, `complex`, or `heavy`).
        model: Optional explicit model override.
        system_instruction: Optional system instruction or skill text.
        context_files: Optional list of disk files attached to the input prompt.
        backend: Execution backend (`genai`, `antigravity`, or `auto`).
        previous_interaction_id: Optional Interactions API ID for multi-turn chaining.
        max_output_tokens: Optional output token cap for `genai` backend.
        background: Whether to run asynchronously in the Gemini Interactions API backend.
        workspace: Optional workspace directory for `antigravity` backend.
        output_file: Optional file path where `output_text` should be saved.

    Returns:
        Dictionary summarizing execution outcome, model used, and `output_text`.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return {
            "status": "error",
            "error": (
                "GEMINI_API_KEY is not set. Create an API key in Google AI Studio "
                "(https://aistudio.google.com/apikey) and set GEMINI_API_KEY before running api_delegate.py."
            ),
        }

    resolved_model = resolve_model(task_tier=task_tier, model=model)
    try:
        full_input = _build_full_input(prompt=prompt, context_files=context_files)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}

    chosen_backend = backend.strip().lower()
    if chosen_backend == "auto":
        chosen_backend = "antigravity" if workspace else "genai"

    try:
        if chosen_backend == "genai":
            result = _run_genai_interaction(
                prompt=full_input,
                model=resolved_model,
                system_instruction=system_instruction,
                previous_interaction_id=previous_interaction_id,
                max_output_tokens=max_output_tokens,
                background=background,
            )
        elif chosen_backend == "antigravity":
            result = asyncio.run(
                _run_antigravity_async(
                    prompt=full_input,
                    model=resolved_model,
                    system_instruction=system_instruction,
                    workspace=workspace,
                )
            )
        else:
            return {
                "status": "error",
                "error": f"Unsupported backend '{backend}'. Use one of: genai, antigravity, auto.",
            }
    except Exception as exc:
        return {
            "status": "error",
            "backend": chosen_backend,
            "model": resolved_model,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if output_file and result.get("status") == "ok":
        dest = Path(output_file).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(result.get("output_text", ""), encoding="utf-8")
        result["output_file"] = str(dest)

    return result


def _configure_utf8_stdout() -> None:
    """Configure UTF-8 console output on Windows when supported."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for delegating KiCad orchestrator tasks via Gemini APIs.

    Args:
        argv: Optional argument list overriding `sys.argv[1:]`.

    Returns:
        Process exit code (`0` on success, `1` on error).
    """
    _configure_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Delegate KiCad orchestrator subtasks to Google Gemini or Antigravity SDK APIs."
    )
    parser.add_argument("--prompt", help="Inline prompt text for the task.")
    parser.add_argument("--prompt-file", help="Path to a UTF-8 file containing the task prompt.")
    parser.add_argument("--system", help="Inline system instruction.")
    parser.add_argument("--system-file", help="Path to a UTF-8 system instruction or SKILL.md file.")
    parser.add_argument(
        "--context-file",
        action="append",
        default=[],
        help="Disk file path (e.g. output/state_summary.json) to attach to the prompt. Repeatable.",
    )
    parser.add_argument(
        "--task-tier",
        choices=sorted(TIER_MODELS),
        default="simple",
        help="Task complexity tier controlling default Gemini model routing.",
    )
    parser.add_argument("--model", help="Explicit model ID overriding --task-tier.")
    parser.add_argument(
        "--backend",
        choices=("auto", "genai", "antigravity"),
        default="genai",
        help="SDK backend used to execute the request.",
    )
    parser.add_argument("--previous-interaction-id", help="Prior Interactions API ID for continuation.")
    parser.add_argument("--max-output-tokens", type=int, help="Optional output token limit.")
    parser.add_argument("--background", action="store_true", help="Run Gemini interaction in background mode.")
    parser.add_argument("--workspace", help="Workspace directory for the antigravity SDK backend.")
    parser.add_argument("--output-file", help="Optional path to save the model output text.")
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Print configured tier-to-model routing and exit without making network calls.",
    )
    parsed = parser.parse_args(argv)

    if parsed.list_models:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "tier_models": TIER_MODELS,
                    "api_key_configured": bool(
                        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        prompt_text = _read_optional_text(parsed.prompt, parsed.prompt_file)
        system_text = _read_optional_text(parsed.system, parsed.system_file)
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if not prompt_text:
        parser.error("Either --prompt or --prompt-file is required (unless --list-models is set).")

    result = delegate_task(
        prompt=prompt_text,
        task_tier=parsed.task_tier,
        model=parsed.model,
        system_instruction=system_text,
        context_files=parsed.context_file,
        backend=parsed.backend,
        previous_interaction_id=parsed.previous_interaction_id,
        max_output_tokens=parsed.max_output_tokens,
        background=parsed.background,
        workspace=parsed.workspace,
        output_file=parsed.output_file,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
