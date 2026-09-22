"""Create-time staging validation.

The single benchmark is a replay by default, and a replay needs a pinned
dataset — so a dataset profile on a campaign with no verify stage is now the
NORMAL case, not a mistake. The both-or-neither rule for the OPT-IN split still
holds.
"""

from types import SimpleNamespace

from app.api.campaigns import _staging_errors


def _body(**kw) -> SimpleNamespace:
    base = dict(
        verify_top_k=0,
        verify_benchmark_slug="",
        verify_max_run_minutes=180,
        dataset_policy="rebuild_at_start",
        dataset_profile="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_single_stage_replay_with_a_dataset_profile_is_accepted():
    # The default: one benchmark (a replay), its pinned dataset, no verify.
    errors = _staging_errors(_body(dataset_profile="prod-traffic-sample"))
    assert errors == []


def test_the_opt_in_split_still_needs_both_halves():
    # A verify benchmark with no top-k never fires...
    assert any("verify_top_k" in e for e in _staging_errors(
        _body(verify_benchmark_slug="replay-v0")))
    # ...and a top-k with no benchmark re-runs the cheap test and calls it proof.
    assert any("verify_benchmark_slug" in e for e in _staging_errors(
        _body(verify_top_k=2)))


def test_a_full_split_config_is_accepted():
    errors = _staging_errors(_body(
        verify_benchmark_slug="replay-v0", verify_top_k=2,
        dataset_profile="prod-traffic-sample",
    ))
    assert errors == []


def test_an_unknown_dataset_policy_is_still_refused():
    assert any("dataset_policy" in e for e in _staging_errors(_body(dataset_policy="whenever")))
