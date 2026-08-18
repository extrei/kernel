"""Claude Code provider for subscription-backed completions."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
from typing import Any

from .api import MODEL
from .base import Completion, ProviderError

_KERNEL_TOOL_ALLOWLIST = "mcp__kernel__get_view,mcp__kernel__submit_patch"
_FORBIDDEN_ARGUMENTS = {
    "--allow-dangerously-skip-permissions",
    "--dangerously-skip-permissions",
}
_PREFLIGHT_SYSTEM = "Credential preflight only. Use no tools and return exactly ok."


class ClaudeCodeProvider:
    """Complete prompts through a non-interactive Claude Code process."""

    name = "claude-code"

    def __init__(
        self,
        project_root: str | Path,
        *,
        executable: str = "claude",
        run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._project_root = Path(project_root).expanduser().resolve()
        self._executable = executable
        self._run_process = run_process

    def preflight(self) -> None:
        """Run a trivial completion so login failures surface before the loop."""

        self._invoke(
            system=_PREFLIGHT_SYSTEM,
            messages=[{"role": "user", "content": "Reply with ok."}],
        )

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> Completion:
        """Return one parsed Claude Code JSON envelope."""

        return self._invoke(system=system, messages=messages)

    def _invoke(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> Completion:
        prompt = json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        arguments = [
            self._executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            MODEL,
            "--tools",
            "",
            "--allowedTools",
            _KERNEL_TOOL_ALLOWLIST,
            "--append-system-prompt",
            system,
            "--no-session-persistence",
        ]
        if _FORBIDDEN_ARGUMENTS.intersection(arguments):
            raise ProviderError("unsafe Claude Code permission bypass is forbidden")
        try:
            result = self._run_process(
                arguments,
                cwd=self._project_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise ProviderError(f"Claude Code is unavailable: {error}") from error

        try:
            envelope: Any = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            detail = (result.stderr or result.stdout or "no output").strip()
            raise ProviderError(f"Claude Code returned invalid JSON: {detail}") from error
        completion = _completion_from_envelope(envelope)
        if result.returncode != 0:
            detail = (result.stderr or "Claude Code exited unsuccessfully").strip()
            raise ProviderError(detail)
        return completion


def _completion_from_envelope(envelope: Any) -> Completion:
    if not isinstance(envelope, dict):
        raise ProviderError("Claude Code JSON envelope must be an object")
    if envelope.get("type") != "result":
        raise ProviderError("Claude Code JSON envelope is not a result")
    if not isinstance(envelope.get("subtype"), str):
        raise ProviderError("Claude Code JSON envelope has no subtype")
    if type(envelope.get("is_error")) is not bool:
        raise ProviderError("Claude Code JSON envelope has invalid error status")
    num_turns = envelope.get("num_turns")
    if type(num_turns) is not int or num_turns < 0:
        raise ProviderError("Claude Code JSON envelope has invalid turn count")
    if not isinstance(envelope.get("permission_denials"), list):
        raise ProviderError("Claude Code JSON envelope has invalid permission denials")
    if not isinstance(envelope.get("stop_reason"), (str, type(None))):
        raise ProviderError("Claude Code JSON envelope has invalid stop reason")
    result = envelope.get("result")
    if not isinstance(result, str):
        raise ProviderError("Claude Code JSON envelope has no result text")
    if envelope.get("is_error") is True:
        raise ProviderError(result)
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        raise ProviderError("Claude Code JSON envelope has no usage object")
    try:
        input_tokens = int(usage["input_tokens"]) + int(
            usage.get("cache_read_input_tokens", 0)
        )
        output_tokens = int(usage["output_tokens"])
        cost_usd = float(envelope["total_cost_usd"])
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderError("Claude Code JSON envelope has invalid usage") from error
    return Completion(
        text=result,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
