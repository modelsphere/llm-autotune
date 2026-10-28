"""Saved engine logs keep what explains a failure."""

from app.control.orchestrator.container_logs import GAP_MARK, collapse_progress, tail_by_section


def test_a_progress_bar_keeps_its_last_frame():
    text = "loading\nwarmup:   0%|\rwarmup:  50%|\rwarmup: 100%|\r\nready"
    assert collapse_progress(text) == "loading\nwarmup: 100%|\nready"


def test_every_pod_keeps_the_end_of_its_section():
    """Under lws the leader's section comes first: a plain tail of the joined
    log kept only the worker's end, which says the leader went away."""
    leader = "[autotune-run-1-0/sglang]\n" + "load\n" * 2000 + "CUDA out of memory\n"
    worker = "[autotune-run-1-0-1]\n" + "capture\n" * 2000 + "Connection closed by peer\n"
    kept = tail_by_section(leader + worker, 4000)
    assert len(kept) <= 4000
    assert "CUDA out of memory" in kept and "Connection closed by peer" in kept
    assert kept.startswith("[autotune-run-1-0/sglang]\n" + GAP_MARK)
    assert "\n[autotune-run-1-0-1]\n" in kept


def test_one_section_is_a_plain_tail():
    text = "line\n" * 100
    assert tail_by_section(text, 50) == text[-50:]
    assert tail_by_section("short", 50) == "short"
