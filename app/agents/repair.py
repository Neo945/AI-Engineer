"""Structured test-and-repair loop for the tester pipeline stage.

The tester no longer trusts the model to run tests and judge the output by
eye. Instead a :class:`RepairAgent` drives a deterministic loop: run the suite
with the ``test_run`` tool, parse the structured failures, hand them to a
repair :class:`LoopAgent` to fix, and re-run. Iterations are bounded by
``max_repairs``; the final answer is a ``VERDICT: PASS|FAIL`` line (parsed by
the pipeline router) followed by the last test report.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.agents.base import DEFAULT_MAX_STEPS, LoopAgent
from app.executor.executor import ToolExecutor, _detect_test_command
from app.executor.test_parser import TestReport, format_report
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider
from app.tools.schemas import ToolCall, ToolName

_REPAIR_PROMPT = (
    "You are a repair engineer working inside a user's repository. The test "
    "suite currently has failing tests; the structured failure report is in "
    "the conversation. Inspect the failing code and fix it with the "
    "workspace tools. Do NOT run the test suite yourself — the system "
    "re-runs it after your turn and reports the result. When you are done, "
    "end your turn with a concise summary of the fixes you made."
)


@dataclass
class RepairResult:
    """Outcome of a test-and-repair loop.

    Attributes:
        answer: Final ``VERDICT: PASS|FAIL`` message.
        messages: The full transcript (seeded conversation plus every message
            produced by the loop), ready for persistence.
        input_tokens: Total input tokens across all LLM calls.
        output_tokens: Total output tokens across all LLM calls.
        steps: Number of LLM calls made (repair turns only).
        repairs: How many fix → re-run iterations were performed.
        report: The final test report that decided the verdict.
    """

    answer: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int
    repairs: int
    report: TestReport | None = None


class RepairAgent:
    """Run the suite, fix failures, and re-run until green or out of attempts.

    Args:
        llm: The LLM provider to call.
        executor: Executor for the workspace being repaired.
        max_repairs: Upper bound on fix → re-run iterations.
        max_steps: Per-repair bound on LLM calls.
        max_tokens: Cap on generated tokens per LLM call.
        temperature: Sampling temperature.
        on_message: Optional callback invoked with every produced message in
            transcript order, as it is produced.
        should_cancel: Optional predicate checked at each repair's step
            boundaries; when ``True`` the run raises :class:`TaskCancelled`.
        test_command: Override for the test command; auto-detected when
            ``None``.
    """

    def __init__(
        self,
        *,
        llm: LLMProvider,
        executor: ToolExecutor,
        max_repairs: int = 2,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        on_message: Callable[[ChatMessage], Awaitable[None] | None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        test_command: str | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._max_repairs = max_repairs
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._on_message = on_message
        self._should_cancel = should_cancel
        self._test_command = test_command

    async def run(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> RepairResult:
        """Run the loop for a goal, seeding it as the leading user message."""
        seeded = [
            ChatMessage(role=ChatRole.USER, content=goal),
            *initial_messages,
        ]
        for message in seeded:
            await self._invoke_on_message(message)
        return await self.run_from(seeded)

    async def run_from(self, messages: Sequence[ChatMessage]) -> RepairResult:
        """Run the loop over an existing conversation.

        Unlike :meth:`run`, the passed-in ``messages`` are not re-emitted
        through ``on_message``; only newly produced messages are. This lets
        the pipeline hand the repair loop the accumulated transcript and
        append only the delta.
        """
        command, framework = self._resolve_test_command()
        transcript = list(messages)
        input_tokens = 0
        output_tokens = 0
        steps = 0
        repairs = 0
        report = await self._run_tests(command, framework)

        repair_agent = LoopAgent(
            llm=self._llm,
            executor=self._executor,
            system_prompt=_REPAIR_PROMPT,
            max_steps=self._max_steps,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            on_message=self._on_message,
            should_cancel=self._should_cancel,
        )

        while not report.ok and repairs < self._max_repairs:
            failure_message = ChatMessage(
                role=ChatRole.USER,
                content=f"The test suite still has failures:\n{format_report(report)}",
            )
            await self._invoke_on_message(failure_message)
            transcript.append(failure_message)
            result = await repair_agent.run_from(transcript)
            transcript = list(result.messages)
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            steps += result.steps
            repairs += 1
            report = await self._run_tests(command, framework)

        verdict = "VERDICT: PASS" if report.ok else "VERDICT: FAIL"
        summary = f"{verdict}\n{format_report(report)}"
        final_message = ChatMessage(role=ChatRole.ASSISTANT, content=summary)
        await self._invoke_on_message(final_message)
        transcript.append(final_message)
        return RepairResult(
            answer=summary,
            messages=transcript,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            steps=steps,
            repairs=repairs,
            report=report,
        )

    def _resolve_test_command(self) -> tuple[str, str]:
        if self._test_command:
            return self._test_command, "pytest"
        return _detect_test_command(self._executor.workspace_dir)

    async def _run_tests(self, command: str, framework: str) -> TestReport:
        result = await self._executor.execute(
            ToolCall(
                tool=ToolName.TEST_RUN,
                arguments={"command": command, "framework": framework},
            )
        )
        payload = result.data.get("report")
        if payload:
            return TestReport.from_dict(payload)
        return TestReport(
            framework=framework,
            command=command,
            failed=0 if result.ok else 1,
            failures=[],
            output=result.output,
        )

    async def _invoke_on_message(self, message: ChatMessage) -> None:
        if self._on_message is None:
            return
        result = self._on_message(message)
        if result is not None:
            await result
