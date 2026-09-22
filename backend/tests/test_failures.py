"""The config/infrastructure split. Everything downstream turns on it: whether
to retry, and whether a searcher should learn to avoid a region."""

from app.control.launch.failures import (
    classify_exit,
    classify_failure,
    is_config_induced,
    is_infrastructure,
)


def test_a_config_failure_is_not_retried_and_is_an_observation():
    """An OOM reproduces if you run the same parameters again — it is a fact
    about the config, and a searcher that never hears it proposes it twice."""
    assert is_config_induced("oom")
    assert not is_infrastructure("oom")


def test_an_infrastructure_failure_is_retried_and_teaches_nothing():
    """Losing a config to a transient ssh timeout means the night silently
    tests less than it was asked to, and the parameters were blameless."""
    assert is_infrastructure("ssh_timeout")
    assert not is_config_induced("ssh_timeout")


def test_our_own_bug_is_in_neither_bucket():
    """`supervisor_error` is deterministic, so retrying burns ~50 minutes per
    attempt — but it says nothing about the config either. It stays visible
    and unlearned-from."""
    assert not is_infrastructure("supervisor_error")
    assert not is_config_induced("supervisor_error")


def test_a_clean_termination_is_not_a_verdict():
    """A stop, a window cutoff or a redeploy sends SIGTERM. Reading that as a
    bad config would poison a searcher with the operator's own decisions."""
    assert classify_exit(143, oom_killed=False) == "terminated"
    assert not is_config_induced("terminated")
    assert not is_infrastructure("terminated")


def test_the_host_oom_killer_counts_as_oom():
    """Nothing reaches the container log when the kernel reaps it; only the
    exit status knows."""
    assert classify_exit(None, oom_killed=True) == "oom"
    assert classify_exit(137, oom_killed=False) == "killed_externally"
    assert classify_exit(0, oom_killed=False) is None


def test_log_classification_covers_the_classes_seen_live():
    assert classify_failure("torch.cuda.OutOfMemoryError: CUDA out of memory") == "oom"
    assert classify_failure("error: unrecognized arguments --nope") == "bad_config"
    assert classify_failure("bind: address already in use") == "port_conflict"
    assert classify_failure("Unable to find image 'x' locally") == "image_missing"
    assert classify_failure("nothing familiar here") == "unknown"
