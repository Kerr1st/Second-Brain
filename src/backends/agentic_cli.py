"""Shared base for agentic-CLI backends.

Every agentic-CLI backend (Kiro's ``kiro-cli chat``, Claude Code's ``claude
-p``, Codex's ``codex exec``) drives a child process the same way: spawn it with
lenient decoding, map a timeout to :class:`TimeoutError` and a non-zero exit to
:class:`RuntimeError`, append one per-call metrics line, dump the raw output for
debugging, parse the final text with the shared :func:`parse_json_output`
backstop, and clean up any temp config in a ``finally``.

:class:`AgenticCliInvoker` owns those mechanics once so the adapters cannot drift
on failure-mode handling (the exact drift Req 8/17 forbid). Each adapter
overrides only the parts that genuinely differ:

* command construction (:meth:`_build_command`)
* system-prompt delivery / temp config (:meth:`_build_config`,
  :meth:`_config_path`, :meth:`_build_stdin`)
* final-text extraction (:meth:`_extract_raw`)
* usage extraction (:meth:`_extract_usage`)
* failure-mode / envelope mapping (:meth:`_check_returncode`, :meth:`_run_probe`)
* the retry policy (:meth:`_attempts_allowed`, :meth:`_should_retry`)

The extraction is constrained to be **behavior-identical for the Kiro path**
(Req 20): :class:`src.backends.kiro.KiroInvoker` is refactored to drive this
template while keeping its exact command surface. See ``docs/MODEL-BACKENDS.md``
and the "Shared base: AgenticCliInvoker" design section.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.backends.base import (
    InvocationBase, write_private_text,
    assert_corpus_route, corpus_role_for_stage, CorpusRoutingError, BACKEND_CAPABILITIES,
    _write_metrics_line as _write_metrics_line,
    parse_json_output as _parse_json_output,
)

logger = logging.getLogger(__name__)

# --- Phase 0 LLM-call instrumentation (see docs/FABLE5-THINKER-PLAN.md) ---
# Canonical definitions now live in src.backends.base (backend-agnostic).
class AgenticCliInvoker(InvocationBase):
    """Template-method base for agentic-CLI backends.

    Implements the :class:`src.backends.base.Invoker` contract via a shared
    :meth:`invoke` that orchestrates spawn → (optional retry) → failure mapping →
    parse → usage → metrics → cleanup. Subclasses set :attr:`BACKEND_NAME` /
    :attr:`DEFAULT_MODEL` and override the hook methods that differ per backend.
    """

    #: Short backend label, used in metrics, log lines, and the raw-debug dump
    #: filename (``f"{BACKEND_NAME}_agent_{name}_raw.txt"``).
    BACKEND_NAME = "agentic_cli"
    #: Default model id when constructed without an explicit ``model``.
    DEFAULT_MODEL = ""

    def __init__(self, model: str | None = None):
        self.model = model or self.DEFAULT_MODEL

    # The JSON recovery backstop is backend-agnostic and shared from base.py.
    parse_json_output = staticmethod(_parse_json_output)

    # ------------------------------------------------------------------ #
    # Template method                                                    #
    # ------------------------------------------------------------------ #
    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        tools: bool = False,
        timeout: int = 300,
        effort: str | None = None,
        stage: str | None = None,
        run_id: str | None = None,
        **adapter_opts,
    ) -> dict:
        """Run one agent turn and return a normalized :class:`InvocationResult`.

        Spawns the CLI subprocess (optionally retrying per the adapter's policy),
        maps timeout → :class:`TimeoutError` and non-zero exit → :class:`RuntimeError`,
        parses the final text via the shared backstop, records one metrics line,
        and cleans up any temp config in a ``finally``.

        Raises:
            TimeoutError: If the subprocess exceeds ``timeout``.
            RuntimeError: If the subprocess fails (non-zero exit, or — for
                tool-using calls — MCP tools failed to attach).
            ValueError: If no JSON is recoverable from the final text.
        """
        corpus_role = corpus_role_for_stage(stage)
        if corpus_role:
            assert_corpus_route(self.BACKEND_NAME, self.model, corpus_role)
            if tools and self.BACKEND_NAME != "claude_code":
                raise CorpusRoutingError("Corpus tool calls require an enforced read-only tool surface")
        needs_tools = tools
        name = self._make_invocation_name()
        config = self._build_config(name, system_prompt, needs_tools)
        config_path = self._config_path(name) if config is not None else None

        metrics = self._init_metrics(
            name=name,
            system_prompt=system_prompt,
            user_message=user_message,
            needs_tools=needs_tools,
            timeout=timeout,
            effort=effort,
            stage=stage,
            run_id=run_id,
        )
        t0 = time.monotonic()
        config_written = False

        try:
            if config is not None:
                self._write_config(config_path, config)
                config_written = True

            cmd = self._build_command(
                name=name,
                config_path=config_path,
                system_prompt=system_prompt,
                user_message=user_message,
                needs_tools=needs_tools,
                effort=effort,
                **adapter_opts,
            )
            stdin = self._build_stdin(
                system_prompt=system_prompt,
                user_message=user_message,
                needs_tools=needs_tools,
            )

            logger.debug(
                "Invoking %s agent: name=%s, timeout=%ds, tools=%s",
                self.BACKEND_NAME, name, timeout, needs_tools,
            )

            attempts_allowed = self._attempts_allowed(needs_tools=needs_tools, **adapter_opts)
            backoff = self._initial_backoff(**adapter_opts)
            result = None
            for attempt in range(1, attempts_allowed + 1):
                try:
                    result = self._run_subprocess(cmd, timeout=timeout, stdin=stdin)
                except TimeoutError:
                    metrics["error"] = "timeout"
                    raise

                if attempt < attempts_allowed and self._should_retry(
                    result,
                    needs_tools=needs_tools,
                    attempt=attempt,
                    attempts_allowed=attempts_allowed,
                    metrics=metrics,
                    backoff=backoff,
                ):
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                break

            metered = BACKEND_CAPABILITIES[self.BACKEND_NAME].metered
            usage, usage_source = (None, "estimate")
            if metered:
                usage, usage_source = self._extract_usage(
                    parsed=None, raw="", result=result, metrics=metrics
                )
            try:
                self._check_returncode(result, metrics)
                self._note_model_honored(result, metrics)
                raw = self._extract_raw(result)
                metrics["output_chars"] = len(raw)
                logger.debug("%s agent raw output length: %d chars", self.BACKEND_NAME, len(raw))
                self._dump_raw_debug(name, raw)
                parsed = self._parse(raw, metrics)
                if not metered:
                    usage, usage_source = self._extract_usage(
                        parsed=parsed, raw=raw, result=result, metrics=metrics
                    )
                self._run_probe(
                    name=name, needs_tools=needs_tools, result=result, parsed=parsed, raw=raw
                )
            except Exception as exc:
                metrics["_usage"] = usage
                metrics["usage_source"] = usage_source
                if metered and usage is not None:
                    exc.model_response_received = True
                    exc.usage, exc.usage_source = usage, usage_source
                    if getattr(exc, "usage_pricing_unknown", False):
                        exc.usage = metrics["_usage"] = None
                        exc.usage_source = metrics["usage_source"] = "unavailable"
                raise

            metrics["_usage"] = usage
            metrics["success"] = True
            metrics["usage_source"] = usage_source
            return {"output": parsed, "raw": raw, "usage": usage, "usage_source": usage_source}

        finally:
            metrics["latency_s"] = round(time.monotonic() - t0, 3)
            metrics["est_input_tokens"] = metrics["input_chars"] // 4
            metrics["est_output_tokens"] = metrics["output_chars"] // 4
            self._record_metrics(metrics, run_id)
            if config_written:
                self._cleanup(config_path)

    # ------------------------------------------------------------------ #
    # Shared mechanics (the base owns these)                             #
    # ------------------------------------------------------------------ #
    def _run_subprocess(self, cmd, *, timeout: int, stdin: Optional[str] = None):
        """Spawn the CLI subprocess with lenient decoding.

        Decodes with ``encoding="utf-8", errors="replace"`` so a stray non-UTF-8
        byte becomes the replacement char instead of crashing the run. Maps
        :class:`subprocess.TimeoutExpired` to :class:`TimeoutError`.
        """
        kwargs = dict(
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if stdin is not None:
            kwargs["input"] = stdin
        try:
            return subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            logger.error("Agent timed out after %ds: %s", timeout, stderr)
            raise TimeoutError(
                f"Agent subprocess timed out after {timeout}s: {stderr}"
            ) from exc

    # Emitted by kiro-cli >= 2.22 (observed 2026-09-17) when the server ignores
    # --model and picks one itself ("Auto"). The call still succeeds, but the
    # model recorded in provenance is then a request, not a fact.
    _MODEL_NOT_HONORED_RE = re.compile(r"failed to set model '([^']*)'", re.I)

    def _note_model_honored(self, result, metrics: dict) -> None:
        stderr = getattr(result, "stderr", "") or ""
        m = self._MODEL_NOT_HONORED_RE.search(stderr)
        metrics["model_honored"] = m is None
        if m is not None:
            logger.warning(
                "%s backend did not honor model '%s' (server-selected model used); "
                "provenance for this call is the requested model, not the served one",
                self.BACKEND_NAME, m.group(1),
            )
            if corpus_role_for_stage(metrics.get("stage")):
                metrics["error"] = "model_not_honored"
                raise CorpusRoutingError("Requested corpus model was not honored")

    def _check_returncode(self, result, metrics: dict) -> None:
        """Map a non-zero exit to :class:`RuntimeError`. Shared failure mode."""
        if result.returncode != 0:
            metrics["error"] = f"exit_code={result.returncode}"
            logger.error(
                "Agent exited with code %d: %s", result.returncode, result.stderr
            )
            raise RuntimeError(
                f"Agent subprocess failed (exit code {result.returncode}): "
                f"{result.stderr}"
            )


    def _dump_raw_debug(self, name: str, raw: str) -> None:
        """Explicit opt-in only; private directory and exclusive 0600 files."""
        import tempfile
        from pathlib import Path
        debug_dir = os.environ.get("SECOND_BRAIN_RAW_DEBUG_DIR")
        if not debug_dir:
            return
        directory = Path(debug_dir).expanduser()
        try:
            if not directory.is_absolute() or directory.is_symlink():
                raise OSError("Raw debug directory must be absolute and not a symlink")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_mode & 0o077:
                raise OSError("Raw debug directory must have mode 0700")
            fd, path = tempfile.mkstemp(prefix="model-response-", suffix=".txt", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(raw)
        except OSError:
            logger.warning("Private raw debug output unavailable")


    def _write_config(self, path: str, config) -> None:
        """Write the temp agent/config file as JSON (default delivery)."""
        write_private_text(path, json.dumps(config))

    def _cleanup(self, path: str) -> None:
        """Remove the temp config file. Never raises."""
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Hooks adapters override                                            #
    # ------------------------------------------------------------------ #
    def _make_invocation_name(self) -> str:
        """Unique per-invocation name (config filename / metrics agent_name)."""
        return f"{self.BACKEND_NAME}_{uuid.uuid4().hex}"

    def _build_config(self, name: str, system_prompt: str, needs_tools: bool):
        """Return the temp-config payload to write, or ``None`` for no file."""
        return None

    def _config_path(self, name: str) -> str:
        """Filesystem path for the temp config file (only used if non-None)."""
        raise NotImplementedError

    def _build_command(
        self,
        *,
        name: str,
        config_path: Optional[str],
        system_prompt: str,
        user_message: str,
        needs_tools: bool,
        effort: str | None,
        **adapter_opts,
    ) -> list:
        """Construct the CLI argv. Adapter-specific.

        Adapter-specific keyword options passed to :meth:`invoke` (e.g. Claude
        Code's ``json_schema``) are forwarded here via ``adapter_opts`` so an
        adapter can shape its command without the base needing to know them.
        """
        raise NotImplementedError

    def _build_stdin(
        self, *, system_prompt: str, user_message: str, needs_tools: bool
    ) -> Optional[str]:
        """Optional stdin payload for the subprocess (default: none)."""
        return None

    def _extract_raw(self, result) -> str:
        """Final text from the completed process (default: stdout)."""
        return result.stdout

    def _extract_usage(self, *, parsed, raw, result, metrics):
        """Return ``(usage, usage_source)``. Default: no real usage."""
        return None, "estimate"

    def _run_probe(self, *, name, needs_tools, result, parsed, raw) -> None:
        """Fail-loud MCP reachability probe for tool-using calls (default: none)."""
        return None

    def _adapter_metrics_defaults(self) -> dict:
        """Adapter-specific extra metrics fields (default: none)."""
        return {}

    def _raw_debug_filename(self, name: str) -> str:
        """Filename for the raw-output debug dump under /tmp."""
        return f"{self.BACKEND_NAME}_agent_{name}_raw.txt"

    # ------------------------------------------------------------------ #
    # Retry policy hooks (default: a single attempt, no retry)           #
    # ------------------------------------------------------------------ #
    def _attempts_allowed(self, *, needs_tools: bool, **_opts) -> int:
        """Total attempts allowed (default: 1, no retry)."""
        return 1

    def _initial_backoff(self, **_opts) -> float:
        """Initial retry backoff in seconds (default: 0)."""
        return 0.0

    def _should_retry(
        self, result, *, needs_tools, attempt, attempts_allowed, metrics, backoff
    ) -> bool:
        """Whether to retry after ``result`` (default: never)."""
        return False
