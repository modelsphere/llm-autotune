"""A delegated-only policy is not handed the weights.

Every policy container used to get the campaign's model mounted at /model
whether it read it or not. Most do not: a delegated-only policy proposes configs
and asks the platform to launch the engines. On a cluster that unused mount is
actively harmful — weights are a per-node hostPath, so mounting them pins a
controller that needs no placement and no GPU onto contended GPU nodes.
"""

from app.control.engines import MODEL_MOUNT
from app.control.orchestrator.policy_session import policy_volumes
from app.db.models import Campaign, Policy


def _campaign(**over):
    base = dict(
        owner_id=1, name="c", engine="sglang", image="img",
        model_path="/mnt/disk0/models/qwen", served_model_name="m",
        extra_volumes={"/host/cache": "/cache"},
    )
    base.update(over)
    return Campaign(**base)


def _policy(needs_model: bool):
    return Policy(owner_id=1, name="p", image="policy:1", needs_model=needs_model)


def test_a_delegated_policy_gets_no_model_mount():
    volumes = policy_volumes(_campaign(), _policy(needs_model=False))
    assert "/mnt/disk0/models/qwen" not in volumes
    assert MODEL_MOUNT not in "".join(volumes.values())


def test_a_self_serving_policy_still_gets_it():
    volumes = policy_volumes(_campaign(), _policy(needs_model=True))
    assert volumes["/mnt/disk0/models/qwen"] == f"{MODEL_MOUNT}:ro"


def test_extra_volumes_survive_either_way():
    for needs_model in (True, False):
        volumes = policy_volumes(_campaign(), _policy(needs_model))
        assert volumes["/host/cache"] == "/cache"


def test_a_campaign_volume_may_override_the_model_mount():
    """extra_volumes wins on a key collision, as it did before this split."""
    campaign = _campaign(extra_volumes={"/mnt/disk0/models/qwen": "/elsewhere"})
    volumes = policy_volumes(campaign, _policy(needs_model=True))
    assert volumes["/mnt/disk0/models/qwen"] == "/elsewhere"


def test_the_default_keeps_the_old_behaviour():
    """Every policy registered before this field existed was launched with the
    mount, so the column defaults to keeping it."""
    plain = Policy(owner_id=1, name="p", image="i")
    assert policy_volumes(_campaign(), plain) == {
        "/mnt/disk0/models/qwen": f"{MODEL_MOUNT}:ro", "/host/cache": "/cache"
    }


def test_an_unflushed_policy_still_gets_the_mount():
    """A column default lands at INSERT, so an object built in memory carries
    None. Reading that as "no model" would drop the mount for exactly the
    self-serving policies that cannot run without it."""
    unflushed = Policy(owner_id=1, name="p", image="i")
    assert unflushed.needs_model is None
    assert policy_volumes(_campaign(), unflushed)["/mnt/disk0/models/qwen"] == f"{MODEL_MOUNT}:ro"
