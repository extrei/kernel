"""Codex CLI provider for read-only, schema-bound worker completions."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .base import Completion, JSON_PATCH_OUTPUT_SCHEMA, ProviderError

CODEX_MODEL = "gpt-5.6-sol"
CODEX_CONFIG = ["-c", 'model_reasoning_effort="max"']
_FORBIDDEN_ARGUMENTS = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
}
_PREFLIGHT_SYSTEM = "Credential preflight only. Return exactly an empty JSON Patch array."


class CodexProvider:
    """Complete worker prompts through a sandboxed ``codex exec`` process."""

    name = "codex"

    def __init__(
        self,
        project_root: str | Path,
        *,
        executable: str = "codex",
        run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._project_root = Path(project_root).expanduser().resolve()
        self._executable = executable
        self._run_process = run_process

    def preflight(self) -> None:
        """Run a trivial read-only completion so authentication fails early."""

        self._invoke(
            system=_PREFLIGHT_SYSTEM,
            messages=[{"role": "user", "content": "Return []."}],
        )

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> Completion:
        """Return the final schema-bound message and reported token usage."""

        return self._invoke(system=system, messages=messages)

    def _invoke(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> Completion:
        prompt = json.dumps(
            {"messages": messages, "system": system},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary_directory = tempfile.TemporaryDirectory(prefix="kernel-codex-")
        try:
            temporary = Path(temporary_directory.name)
            schema_path = temporary / "patch-schema.json"
            output_path = temporary / "last-message.json"
            schema_path.write_text(
                json.dumps(
                    JSON_PATCH_OUTPUT_SCHEMA,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            arguments = [
                self._executable,
                "exec",
                "--model",
                CODEX_MODEL,
                *CODEX_CONFIG,
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                str(self._project_root),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--json",
                prompt,
            ]
            if _FORBIDDEN_ARGUMENTS.intersection(arguments):
                raise ProviderError("unsafe Codex sandbox bypass is forbidden")
            try:
                result = self._run_process(
                    arguments,
                    cwd=self._project_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as error:
                raise ProviderError(f"Codex is unavailable: {error}") from error
            if result.returncode != 0:
                detail = (result.stderr or "codex exec exited unsuccessfully").strip()
                raise ProviderError(f"codex exec failed: {detail}")
            try:
                text = output_path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ProviderError("codex exec produced no final message") from error
            if not text:
                raise ProviderError("codex exec produced an empty final message")
            input_tokens, output_tokens = _usage_from_jsonl(result.stdout)
            return Completion(
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=None,
            )
        finally:
            temporary_directory.cleanup()


def _usage_from_jsonl(value: str) -> tuple[int, int]:
    usage: tuple[int, int] | None = None
    for line in value.splitlines():
        try:
            event: Any = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        record = event.get("usage")
        if not isinstance(record, dict):
            continue
        input_tokens = record.get("input_tokens")
        output_tokens = record.get("output_tokens")
        if (
            type(input_tokens) is int
            and input_tokens >= 0
            and type(output_tokens) is int
            and output_tokens >= 0
        ):
            usage = (input_tokens, output_tokens)
    return (0, 0) if usage is None else usage
