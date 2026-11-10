from omnigent.loop_guard import BURST_HOPS, MAX_HOPS, evaluate, message_digest

LONG = "finding " + "x" * 300


def _run(messages, *, now=1000):
    prev_hops, stamps, recent, verdict = 0, [], [], None
    for index, text in enumerate(messages):
        verdict = evaluate(
            text,
            prev_hops=prev_hops,
            last_at=now + index - 1,
            stamps=stamps,
            recent=recent,
            now=now + index,
        )
        if verdict.tripped:
            return verdict
        prev_hops, stamps, recent = verdict.hops, verdict.stamps, verdict.recent
    return verdict


def test_short_messages_count_and_the_seventh_trips():
    assert _run(["ok"] * MAX_HOPS).tripped is None
    assert _run(["ok"] * (MAX_HOPS + 1)).tripped == "loop"


def test_long_distinct_messages_do_not_count():
    verdict = _run([f"{LONG} {n}" for n in range(MAX_HOPS * 2)])
    assert verdict.tripped is None and verdict.hops == 0


def test_repeating_a_recent_long_message_counts_despite_case_and_spacing():
    repeats = [LONG.upper(), "  " + LONG.replace(" ", "   ")] * 4
    assert _run([LONG, *repeats]).tripped == "loop"
    assert message_digest("[Message from a (session s)]\nHello  There") == message_digest(
        "hello there"
    )


def test_burst_backstop_holds_the_message_after_twenty_in_the_window():
    assert _run([f"{LONG} {n}" for n in range(BURST_HOPS)]).tripped is None
    assert _run([f"{LONG} {n}" for n in range(BURST_HOPS + 1)]).tripped == "burst"


def test_a_quiet_window_restarts_the_counters():
    verdict = evaluate("ok", prev_hops=MAX_HOPS, last_at=0, stamps=[0] * 30, recent=[], now=5000)
    assert verdict.tripped is None and verdict.hops == 1
