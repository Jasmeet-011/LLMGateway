"""Content checks of `loadtest/run.ps1`'s power guard (M18, D-121, "Guard in run.ps1", as amended
on 2026-11-13 after the first pass).

D-121 says the script: (1) before anything else reads the System log's latest Kernel-Power event,
506 or 507, and refuses to start when it is a 506 (the machine is in standby); (2) from then until
it finishes holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
ES_DISPLAY_REQUIRED)` (0x80000003), released in a `finally`; (3) after the run lists every
Kernel-Power 506 and 507 event between the run's start and end in run.json as `power_events`
(each `{"utc", "id"}`); (4) if there is any, saves the run's output and then stops with an error.

A content test cannot run PowerShell, Windows' event log or the Win32 call. What it can honestly
pin is that each named element is present in the code (comments stripped), in the order D-121
gives. It cannot prove: that the query returns the latest event (not the oldest), that a 506 is
really refused (the condition is right), that the flags reach the call as 0x80000003 when they are
built from named constants, that the release really runs on every exit path, that run.json's
list has the right members and shape (`[]` when empty, a list when one event), that the times are
UTC, or that the final error is raised only when events occurred. Those need a run on the owner's
machine (the guard can be exercised by a short `-Smoke` run, then with the laptop put to sleep).
"""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "loadtest" / "run.ps1"
STANDBY = 0x80000003
CONTINUOUS = 0x80000000


def code() -> str:
    """run.ps1 with its comment-based help block and whole-line `#` comments removed."""
    text = SCRIPT.read_text(encoding="utf-8")
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines)


def first_docker_call(text: str) -> int:
    match = re.search(r"Invoke-Native\s+'docker'|&\s*docker\b", text)
    assert match is not None, "run.ps1 never calls docker"
    return match.start()


def power_reads(text: str) -> list[re.Match[str]]:
    reads = list(re.finditer(r"Kernel-Power", text))
    assert reads, "run.ps1 never reads Kernel-Power events"
    return reads


def blocks(text: str, keyword: str) -> list[str]:
    """The bodies of every `<keyword> { ... }`, found by brace counting."""
    found: list[str] = []
    for match in re.finditer(rf"\b{keyword}\s*\{{", text):
        depth = 1
        index = match.end()
        while depth and index < len(text):
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        found.append(text[match.end() : index - 1])
    return found


def hex_values(text: str) -> set[int]:
    return {int(m.group(1), 16) for m in re.finditer(r"\b0x([0-9a-fA-F]+)u?\b", text)}


def execution_state_calls(text: str) -> list[tuple[int, str]]:
    """(position, argument text) of each call of SetThreadExecutionState, not its declaration."""
    calls = []
    for match in re.finditer(r"SetThreadExecutionState\s*\(([^)]*)\)", text):
        argument = match.group(1).strip()
        if re.fullmatch(r"(u?int|uint32|int32)\s+\w+", argument, re.IGNORECASE):
            continue  # the P/Invoke declaration: `uint SetThreadExecutionState(uint flags)`
        calls.append((match.start(), argument))
    return calls


def holds_standby_off(argument: str) -> bool:
    """True when the argument is ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED."""
    if STANDBY in hex_values(argument):
        return True
    return all(name in argument for name in ("ES_CONTINUOUS", "ES_SYSTEM_REQUIRED")) and (
        "ES_DISPLAY_REQUIRED" in argument
    )


def releases(argument: str) -> bool:
    """True when the argument is ES_CONTINUOUS alone: the request is cleared."""
    if "REQUIRED" in argument or STANDBY in hex_values(argument):
        return False
    return CONTINUOUS in hex_values(argument) or "ES_CONTINUOUS" in argument


def test_the_script_exists_and_has_code_after_stripping_comments() -> None:
    assert SCRIPT.is_file()
    assert "Invoke-Native" in code()


# --- (1) the guard reads Kernel-Power 506 and 507 and refuses to start in standby --------------


def test_the_script_reads_kernel_power_events_506_and_507_from_the_system_log() -> None:
    text = code()
    assert power_reads(text), "run.ps1 never reads Kernel-Power events"
    assert re.search(r"['\"]System['\"]", text), "no read of the System log"
    assert re.search(r"\b506\b", text)
    assert re.search(r"\b507\b", text)


def test_the_kernel_power_read_comes_before_anything_the_run_does() -> None:
    text = code()
    read = power_reads(text)[0].start()
    assert read < first_docker_call(text)
    assert read < text.index("New-Item")
    assert read < text.index("Test-Path")


def test_the_script_refuses_to_start_when_the_latest_event_is_506_before_any_docker_call() -> None:
    text = code()
    docker = first_docker_call(text)
    read = power_reads(text)[0].start()
    between = text[read:docker]
    # A comparison with 506 and a throw, both after the read and before the first docker call.
    assert re.search(r"(-eq|-ne|-in|-contains)[^\n]*\b506\b|\b506\b[^\n]*(-eq|-ne|-in)", between)
    assert re.search(r"\bthrow\b", between), "no `throw` between the event read and docker"
    assert re.search(r"\bthrow\b[^\n]*(standby|power|sleep|asleep|suspend)", between, re.I)


# --- (2) the power request: SetThreadExecutionState(0x80000003), released in a finally --------


def test_the_script_calls_set_thread_execution_state_with_continuous_system_and_display() -> None:
    calls = execution_state_calls(code())
    assert calls, "run.ps1 never calls SetThreadExecutionState"
    assert any(holds_standby_off(argument) for _, argument in calls), calls


def test_the_power_request_is_taken_after_the_guard_and_before_the_first_docker_call() -> None:
    text = code()
    held = [pos for pos, arg in execution_state_calls(text) if holds_standby_off(arg)]
    assert held
    assert power_reads(text)[0].start() < held[0] < first_docker_call(text)


def test_the_power_request_is_released_in_a_finally_with_es_continuous_alone() -> None:
    text = code()
    released = [
        argument
        for body in blocks(text, "finally")
        for _, argument in execution_state_calls(body)
        if releases(argument)
    ]
    assert released, "no finally block clears the power request with ES_CONTINUOUS alone"


def test_the_release_is_in_a_finally_that_encloses_the_whole_run() -> None:
    # The finally that releases must come after the last docker call, so nothing runs unprotected.
    text = code()
    last_docker = max(
        m.start() for m in re.finditer(r"Invoke-Native\s+'docker'|&\s*docker\b", text)
    )
    release_positions = []
    for match in re.finditer(r"\bfinally\s*\{", text):
        body = blocks(text[match.start() :], "finally")[0]
        if any(releases(arg) for _, arg in execution_state_calls(body)):
            release_positions.append(match.start())
    assert release_positions
    assert max(release_positions) > last_docker


# --- (3) power_events in run.json --------------------------------------------------------------


def meta_block(text: str) -> str:
    match = re.search(r"\$meta\s*=\s*\[ordered\]@\{", text)
    assert match is not None, "no $meta = [ordered]@{ ... } in run.ps1"
    depth = 1
    index = match.end()
    while depth and index < len(text):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        index += 1
    return text[match.end() : index - 1]


def test_run_json_gets_a_power_events_field() -> None:
    assert re.search(r"^\s*power_events\s*=", meta_block(code()), re.MULTILINE)


def test_each_power_event_carries_a_utc_time_and_an_id() -> None:
    text = code()
    assert re.search(r"\butc\s*=", text), "no `utc =` key for a power event"
    assert re.search(r"@\{[^}]*\butc\s*=[^}]*\bid\s*=|@\{[^}]*\bid\s*=[^}]*\butc\s*=", text, re.S)


def test_the_power_events_are_read_a_second_time_after_the_run_between_start_and_end() -> None:
    text = code()
    reads = power_reads(text)
    # Either the System log is read in two places, or one function that does is called twice.
    if len(reads) >= 2:
        second = reads[-1].start()
    else:
        function = re.search(r"function\s+([\w-]+)[^{]*\{[^}]*Kernel-Power", text, re.S)
        assert function is not None, "Kernel-Power is read once and not in a function"
        calls = [
            m.start()
            for m in re.finditer(rf"(?<!function )\b{re.escape(function.group(1))}\b", text)
        ]
        assert len(calls) >= 2, "the single read is not repeated after the run"
        second = calls[-1]
    assert second > text.index("$ended = "), "no read of the events after the run's end time"
    without_meta = text.replace(meta_block(text), "")
    window = re.search(r"\$started.{0,400}\$ended|\$ended.{0,400}\$started", without_meta, re.S)
    assert window, "the events are not bounded by the run's start and end"


def test_only_events_506_and_507_are_listed_not_every_kernel_power_event() -> None:
    text = code()
    assert re.search(r"\b506\b[\s\S]{0,60}\b507\b|\b507\b[\s\S]{0,60}\b506\b", text)


# --- (4) stop with an error after the output is saved ------------------------------------------


def power_throws(text: str) -> list[re.Match[str]]:
    return [
        m
        for m in re.finditer(r"\bthrow\b[^\n]*", text)
        if re.search(r"power|standby|sleep|suspend|kernel-power", m.group(0), re.I)
    ]


def test_the_script_stops_with_an_error_when_power_events_occurred() -> None:
    text = code()
    # One throw is the pre-start refusal; another is the after-run stop. At least two throws
    # mention the power condition.
    assert len(power_throws(text)) >= 2, [m.group(0) for m in power_throws(text)]


def test_the_stop_comes_after_run_json_is_written_and_every_output_file_is_saved() -> None:
    text = code()
    after_run = [m for m in power_throws(text) if m.start() > first_docker_call(text)]
    assert after_run, "no power throw after the run starts"
    stop = after_run[-1].start()
    for name in ("k6.txt", "api.log", "run.json"):
        write = re.search(rf"Write-LfFile[^\n]*'{re.escape(name)}'", text)
        assert write is not None, name
        assert write.start() < stop, f"the power stop comes before {name} is saved"


def test_the_stop_comes_before_the_script_reports_done() -> None:
    text = code()
    done = text.index('Write-Host "done')
    after_run = [m for m in power_throws(text) if m.start() > first_docker_call(text)]
    assert after_run
    assert after_run[-1].start() < done


def test_the_stop_is_conditional_on_events_not_unconditional() -> None:
    text = code()
    after_run = [m for m in power_throws(text) if m.start() > first_docker_call(text)]
    assert after_run
    preceding = text[max(0, after_run[-1].start() - 300) : after_run[-1].start()]
    assert re.search(r"\bif\s*\(", preceding), "the power throw is not under an `if (...)`"


# --- the guard and the stop, each throw inside its own `if` (strengthened after mutation) -------
#
# The tests above find `throw` and `506` anywhere in a span, so another throw in that span (the
# refused power request) satisfies them. These tie each condition to the throw it guards, by
# reading `if (...) { ... }` as a unit.


def if_blocks(text: str) -> list[tuple[int, str, str]]:
    """(position, condition, body) of every `if (condition) { body }`, by counting."""
    found: list[tuple[int, str, str]] = []
    for match in re.finditer(r"\bif\s*\(", text):
        depth, index = 1, match.end()
        while depth and index < len(text):
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            index += 1
        condition = text[match.end() : index - 1]
        opening = re.compile(r"\s*\{").match(text, index)
        if opening is None:
            continue
        depth, index = 1, opening.end()
        while depth and index < len(text):
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        found.append((match.start(), condition, text[opening.end() : index - 1]))
    return found


STANDBY_WORDS = re.compile(r"standby|power|sleep|asleep|suspend", re.IGNORECASE)


def test_the_refusal_is_one_if_on_the_latest_event_being_506_whose_body_throws() -> None:
    text = code()
    docker = first_docker_call(text)
    guards = [
        (position, condition, body)
        for position, condition, body in if_blocks(text)
        if position < docker and re.search(r"\.Id\s+-eq\s+506\b", condition)
    ]
    assert len(guards) == 1, "expected one `if` before the first docker call on .Id -eq 506"
    _, condition, body = guards[0]
    assert "-ne" not in condition, "the refusal's condition must be `-eq 506`, not `-ne`"
    assert not re.search(r"\b507\b", condition)
    throws = re.findall(r"\bthrow\b[^\n]*", body)
    assert len(throws) == 1, throws
    assert STANDBY_WORDS.search(throws[0]), throws[0]


def test_the_refusal_tests_the_newest_event_read_with_newest_1() -> None:
    text = code()
    assert re.search(r"\$last\s*=\s*@\(\s*Get-StandbyEvents\s+-Newest\s+1\s*\)", text)
    refusal = next(c for _, c, _ in if_blocks(text) if re.search(r"\.Id\s+-eq\s+506\b", c))
    assert "$last" in refusal


def test_the_stop_after_the_run_is_one_if_on_power_events_count_above_zero_with_a_throw() -> None:
    text = code()
    docker = first_docker_call(text)
    stops = [
        (position, condition, body)
        for position, condition, body in if_blocks(text)
        if position > docker and re.search(r"\$powerEvents\b", condition)
    ]
    assert len(stops) == 1, "expected one `if` on $powerEvents after the run starts"
    _, condition, body = stops[0]
    assert re.fullmatch(r"\s*\$powerEvents\.Count\s+-gt\s+0\s*", condition), condition
    throws = re.findall(r"\bthrow\b[^\n]*", body)
    assert len(throws) == 1, throws
    assert STANDBY_WORDS.search(throws[0]), throws[0]
    # It is the only power throw after the run starts, so none sits outside this `if`.
    outside = [m for m in power_throws(text) if m.start() > docker]
    assert len(outside) == 1


def test_no_power_throw_after_the_run_starts_stands_outside_an_if() -> None:
    text = code()
    docker = first_docker_call(text)
    inside = [(p, b) for p, _, b in if_blocks(text) if p > docker]
    for throw in power_throws(text):
        if throw.start() < docker:
            continue
        assert any(p < throw.start() and throw.group(0) in b for p, b in inside), throw.group(0)
