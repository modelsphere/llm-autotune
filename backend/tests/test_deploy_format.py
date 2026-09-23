"""`config/model.yaml` ↔ engine config: read it, plan a winner's changes,
apply them as a human edit would.

The fixtures are the two shapes the deploy repo actually carries: the H100
release branch (quoted items, a comment block above extraArgs) and the B300
one (bare items, no blank line before env:). Every assertion about the edited
text is about what did NOT change as much as what did — the deploy repo's CI
validates this file line by line, so a stray reflow is a broken pipeline.
"""

import textwrap

import pytest

from app.control.deploy_format import ValuesYamlFormat, chart_owned_of, get_format
from app.control.deploy_format.base import unified_diff
from app.control.deploy_format.policy import (
    SHORTCUTS,
    add_equivalence,
    apply_shortcut,
    compare,
    merge_divergences,
    set_policy,
)
from app.control.deploy_format.policy import (
    plan_changes as _plan,
)
from app.control.launch_config import LaunchConfig

FMT = ValuesYamlFormat()
OWNED = chart_owned_of(FMT)


def parse_model_config(text, engine="sglang"):
    return FMT.parse(text, engine)


def plan_changes(
    head,
    winner_args,
    *,
    engine="sglang",
    winner_image="",
    winner_env=None,
    apply_removals=False,
    policy=None,
    equivalences=None,
    promote_fields=(),
):
    target = LaunchConfig(
        engine=engine,
        image=winner_image,
        engine_args=dict(winner_args),
        extra_env=dict(winner_env or {}),
    )
    return _plan(
        head,
        target,
        policy=policy,
        equivalences=equivalences,
        chart_owned=OWNED,
        apply_removals=apply_removals,
        promote_fields=frozenset(promote_fields),
        engine=engine,
    )


def apply_changes(text, head, plan):
    return FMT.apply(text, head, plan)


H100 = textwrap.dedent("""\
    # Model developer configuration layered on the shared SGLang chart.
    # CI resolves image.tag to an immutable digest reference before rendering.
    image:
      repository: registry.example.com/sglang
      tag: v0.5.15-cu129

    model:
      name: kimi
      localPath: /mnt/disk0/models/modelforge/release_260817-794782
      mountPath: /models
      hostPathType: Directory
      contextLength: ""
      gpus: "2"

    modelCheck:
      enabled: true
      requiredGlobs:
        - config.json
        - "*.safetensors"

    service:
      type: ClusterIP
      port: 8050

    # The shared chart supplies the launcher, model path, served name, host, port
    # and metrics flags. Model developers own the remaining engine arguments.
    extraArgs:
      - "--tp-size=2"
      - "--mamba-radix-cache-strategy=extra_buffer"
      - "--chunked-prefill-size=16384"
      - "--mem-fraction-static=0.85"
      - "--enable-cache-report"
      - "--stream-response-default-include-usage"
      - "--reasoning-parser=qwen3"
      - "--tool-call-parser=qwen3_coder"

    env:
      - name: NVIDIA_DISABLE_REQUIRE
        value: "1"
      - name: PYTORCH_ALLOC_CONF
        value: expandable_segments:True

    volumes:
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 16Gi

    volumeMounts:
      - name: dshm
        mountPath: /dev/shm

    affinity:
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: nvidia.com/gpu.product
                  operator: In
                  values:
                    - NVIDIA-H100-80GB-HBM3
                    - NVIDIA-H800
""")

B300 = textwrap.dedent("""\
    image:
      repository: registry.example.com/sglang
      tag: v0.5.19

    model:
      name: kimi-k3
      localPath: /mnt/disk0/models/Kimi-K3
      mountPath: /model
      hostPathType: Directory
      contextLength: ""
      gpus: "8"

    service:
      type: ClusterIP
      port: 8050

    extraArgs:
      - --trust-remote-code
      - --tp-size=8
      - --dcp-size=8
      - --kv-cache-dtype=fp8_e4m3
      - --max-running-requests=36
      - --mem-fraction-static=0.85
      - --speculative-algorithm=DSPARK
      - --speculative-draft-model-path=/draft-model
    env:
      - name: NVIDIA_DISABLE_REQUIRE
        value: "1"
""")


# -- reading -------------------------------------------------------------------


def test_reads_the_h100_file_into_canonical_args():
    parsed = parse_model_config(H100)
    assert parsed.image_repository == "registry.example.com/sglang"
    assert parsed.image_tag == "v0.5.15-cu129"
    assert parsed.image == "registry.example.com/sglang:v0.5.15-cu129"
    assert parsed.model_path == "/mnt/disk0/models/modelforge/release_260817-794782"
    assert parsed.gpus == 2
    assert parsed.gpu_product == "NVIDIA-H100-80GB-HBM3"
    assert parsed.env == {
        "NVIDIA_DISABLE_REQUIRE": "1",
        "PYTORCH_ALLOC_CONF": "expandable_segments:True",
    }
    # Aliases fold to the canonical vocabulary the search space speaks.
    assert parsed.engine_args["tp"] == "2"
    assert parsed.engine_args["mamba_scheduler_strategy"] == "extra_buffer"
    assert parsed.engine_args["mem_fraction_static"] == "0.85"
    assert parsed.engine_args["enable_cache_report"] is True
    assert "tp_size" not in parsed.engine_args
    # The file's own spelling is remembered per knob, for the editor.
    assert parsed.sites["tp"].flag == "--tp-size"
    assert parsed.sites["tp"].quote == '"'
    assert parsed.sites["mamba_scheduler_strategy"].flag == "--mamba-radix-cache-strategy"
    assert not parsed.warnings


def test_reads_bare_list_items():
    parsed = parse_model_config(B300)
    assert parsed.gpus == 8
    assert parsed.engine_args["trust_remote_code"] is True
    assert parsed.engine_args["dcp_size"] == "8"
    assert parsed.sites["tp"].quote == ""


def test_chart_owned_flags_are_kept_out_of_engine_args():
    text = H100.replace(
        '  - "--tp-size=2"\n', '  - "--tp-size=2"\n  - "--port=8050"\n  - "--enable-metrics"\n'
    )
    parsed = parse_model_config(text)
    assert "port" not in parsed.engine_args
    assert "enable_metrics" not in parsed.engine_args
    assert set(parsed.chart_owned_present) == {"port", "enable_metrics"}


# -- planning ------------------------------------------------------------------


def _winner_h100(**overrides):
    base = {
        "tp": 2,
        "mamba_scheduler_strategy": "extra_buffer",
        "chunked_prefill_size": 16384,
        "mem_fraction_static": 0.85,
        "enable_cache_report": True,
        "stream_response_default_include_usage": True,
        "reasoning_parser": "qwen3",
        "tool_call_parser": "qwen3_coder",
    }
    base.update(overrides)
    return base


def test_identical_config_plans_nothing():
    head = parse_model_config(H100)
    plan = plan_changes(head, _winner_h100())
    assert plan.empty
    assert not plan.changes and not plan.kept and not plan.warnings


def test_plans_changed_added_and_switched_off_knobs():
    head = parse_model_config(H100)
    winner = _winner_h100(
        mem_fraction_static=0.9,  # changed
        chunked_prefill_size="16384",  # same value, different type: not a change
        attention_backend="flashinfer",  # added
        enable_cache_report=False,  # switch off = remove the line
        cuda_graph_max_bs=2.0,  # a float-snapped int
    )
    plan = plan_changes(head, winner)
    by_key = {c.key: c for c in plan.changes}
    assert by_key["mem_fraction_static"].kind == "changed"
    assert by_key["mem_fraction_static"].before == "0.85"
    assert by_key["mem_fraction_static"].after == 0.9
    assert by_key["attention_backend"].kind == "added"
    assert by_key["attention_backend"].flag == "--attention-backend"
    assert (
        by_key["enable_cache_report"].kind == "changed"
        and by_key["enable_cache_report"].after is False
    )
    assert "chunked_prefill_size" not in by_key
    assert not plan.kept


def test_a_knob_the_winner_never_mentions_is_kept_unless_asked():
    head = parse_model_config(H100)
    winner = _winner_h100()
    del winner["reasoning_parser"]
    plan = plan_changes(head, winner)
    assert [c.key for c in plan.kept] == ["reasoning_parser"]
    assert not plan.changes
    forced = plan_changes(head, winner, apply_removals=True)
    assert [c.key for c in forced.changes] == ["reasoning_parser"]
    assert forced.changes[0].kind == "removed"


def test_chart_owned_winner_keys_are_never_written():
    head = parse_model_config(H100)
    plan = plan_changes(head, _winner_h100(port=28200, enable_metrics=True))
    assert {i.key for i in plan.ignored} == {"port", "enable_metrics"}
    assert all(i.note == "substrate" for i in plan.ignored)
    assert not plan.changes and not plan.reported


def test_gpus_follow_the_card_count_when_the_file_agrees_with_itself():
    head = parse_model_config(H100)  # gpus "2", tp 2
    plan = plan_changes(head, _winner_h100(tp=4))
    assert (plan.gpus_before, plan.gpus_after) == (2, 4)


def test_gpus_are_left_alone_in_a_layout_the_platform_does_not_model():
    # Kimi-K2.5 style: tp=8 pp=2 over two LWS pods, 8 GPUs per pod.
    text = B300.replace("  - --tp-size=8\n", "  - --tp-size=8\n  - --pp-size=2\n")
    head = parse_model_config(text)
    winner = dict(head.engine_args, tp=2)  # 2×2 = 4 cards, file says gpus "8"
    plan = plan_changes(head, winner)
    assert plan.gpus_after is None
    assert any("gpus left as is" in w for w in plan.warnings)


def test_image_is_repo_owned_by_default_and_only_reported():
    head = parse_model_config(H100)
    plan = plan_changes(
        head, _winner_h100(), winner_image="registry.example.com/sglang:v0.5.19"
    )
    assert not plan.image_tag_after
    assert [r.key for r in plan.reported] == ["image"]
    assert plan.reported[0].before == "registry.example.com/sglang:v0.5.15-cu129"


def test_image_tag_follows_the_winner_when_promoted_and_within_the_same_repository():
    head = parse_model_config(H100)
    follow = apply_shortcut({}, "follow_image")
    same = plan_changes(
        head,
        _winner_h100(),
        winner_image="registry.example.com/sglang:v0.5.19",
        policy=follow,
    )
    assert (same.image_tag_before, same.image_tag_after) == ("v0.5.15-cu129", "v0.5.19")
    ticked = plan_changes(
        head,
        _winner_h100(),
        winner_image="registry.example.com/sglang:v0.5.19",
        promote_fields={"image"},
    )
    assert ticked.image_tag_after == "v0.5.19"
    other = plan_changes(
        head, _winner_h100(), winner_image="lmsysorg/sglang:v0.5.19", policy=follow
    )
    assert not other.image_tag_after
    assert any("image left as is" in w for w in other.warnings)


def test_an_equivalent_image_is_no_difference_at_all():
    head = parse_model_config(H100)
    eq = add_equivalence(
        {},
        field="image",
        ours="sglang:local-v0.5.15",
        theirs="registry.example.com/sglang:v0.5.15-cu129",
    )
    plan = plan_changes(head, _winner_h100(), winner_image="sglang:local-v0.5.15", equivalences=eq)
    assert not plan.reported and not plan.image_tag_after and plan.empty


def test_env_differences_are_reported_not_edited():
    head = parse_model_config(H100)
    plan = plan_changes(
        head, _winner_h100(), winner_env={"PYTORCH_ALLOC_CONF": "x", "NVIDIA_DISABLE_REQUIRE": "1"}
    )
    assert [r.key for r in plan.reported] == ["env:PYTORCH_ALLOC_CONF"]
    ignored = plan_changes(
        head,
        _winner_h100(),
        winner_env={"PYTORCH_ALLOC_CONF": "x"},
        policy=apply_shortcut({}, "ignore_env"),
    )
    assert not ignored.reported


# -- policy --------------------------------------------------------------------


def test_path_valued_knobs_are_repo_owned_until_someone_decides():
    head = parse_model_config(B300)  # --speculative-draft-model-path=/draft-model
    winner = dict(head.engine_args, speculative_draft_model_path="/models/Kimi-K3-DSpark")
    plan = plan_changes(head, winner)
    assert not plan.changes
    assert [r.key for r in plan.reported] == ["speculative_draft_model_path"]
    # Declared equivalent: no difference, and a NEW value would be translated.
    eq = add_equivalence(
        {},
        knob="speculative_draft_model_path",
        ours="/models/Kimi-K3-DSpark",
        theirs="/draft-model",
    )
    assert plan_changes(head, winner, equivalences=eq).empty
    # Ignored: not even reported.
    quiet = plan_changes(head, winner, policy=apply_shortcut({}, "ignore_paths"))
    assert not quiet.reported and [i.key for i in quiet.ignored] == ["speculative_draft_model_path"]


def test_a_repo_owned_knob_is_reported_and_an_ignored_one_is_silent():
    head = parse_model_config(H100)
    winner = _winner_h100(reasoning_parser="deepseek")
    assert [c.key for c in plan_changes(head, winner).changes] == ["reasoning_parser"]
    repo = plan_changes(head, winner, policy=set_policy({}, knob="reasoning_parser", owner="repo"))
    assert not repo.changes and [r.key for r in repo.reported] == ["reasoning_parser"]
    quiet = plan_changes(head, winner, policy=apply_shortcut({}, "ignore_parsers"))
    assert not quiet.changes and not quiet.reported and quiet.ignored[0].key == "reasoning_parser"


def test_shortcuts_compose_and_reject_unknown_names():
    policy = apply_shortcut(apply_shortcut({}, "ignore_image"), "ignore_model_path")
    assert policy["fields"] == {"image": "ignore", "model_path": "ignore"}
    assert set(SHORTCUTS) >= {
        "ignore_image",
        "ignore_model_path",
        "ignore_env",
        "ignore_volumes",
        "ignore_parsers",
        "ignore_paths",
        "follow_image",
        "follow_model_path",
    }
    with pytest.raises(ValueError):
        apply_shortcut({}, "ignore_everything")


def test_compare_sorts_differences_by_owner():
    head = parse_model_config(B300)
    theirs = LaunchConfig.from_parsed(head.as_launch_dict())
    ours = LaunchConfig(
        engine="sglang",
        image="sglang:local",
        model_path="/data/kimi-k3",
        engine_args=dict(
            head.engine_args,
            mem_fraction_static=0.9,
            speculative_draft_model_path="/models/Kimi-K3-DSpark",
        ),
    )
    adopt, divergences = compare(ours, theirs, head, {}, {}, chart_owned=OWNED)
    # platform-owned: adopted; a blank on our side (env) is filled, not argued.
    assert [a["key"] for a in adopt] == ["mem_fraction_static", "extra_env"]
    assert {d["key"]: d["status"] for d in divergences} == {
        "speculative_draft_model_path": "unresolved",  # path heuristic, undecided
        "image": "unresolved",
        "model_path": "unresolved",  # repo by default, undecided
    }
    decided = set_policy(apply_shortcut({}, "ignore_image"), field="model_path", owner="repo")
    _, again = compare(ours, theirs, head, decided, {}, chart_owned=OWNED)
    assert {d["key"]: d["status"] for d in again} == {
        "speculative_draft_model_path": "unresolved",
        "model_path": "repo",
    }
    # A decision recorded on a divergence survives the next sync.
    previous = [
        {**d, "status": "equivalent", "decided_by": "sun"}
        for d in again
        if d["key"] == "speculative_draft_model_path"
    ]
    merged = merge_divergences(previous, again)
    assert {d["key"]: d["status"] for d in merged} == {
        "speculative_draft_model_path": "equivalent",
        "model_path": "repo",
    }


def test_presets_and_a_custom_layout_are_data():
    fmt = get_format({"preset": "helm-release-branch"})
    assert fmt.parse(H100).engine_args["tp"] == "2"
    custom = get_format(
        {
            "adapter": "values_yaml",
            "options": {
                "args": {"path": "sglang.args", "style": "mapping"},
                "image": {"ref": "sglang.image"},
                "gpus": {"path": "sglang.resources.gpus"},
                "model_path": {"path": "sglang.model"},
                "env": {"path": "sglang.env", "style": "mapping"},
            },
        }
    )
    text = textwrap.dedent("""\
        sglang:
          image: registry.example.com/sglang:v0.5.19
          model: /mnt/models/kimi
          resources:
            gpus: 4
          env:
            NCCL_DEBUG: WARN
          args:
            tp-size: 4
            mem-fraction-static: "0.85"
            enable-cache-report: true
        other:
          x: 1
    """)
    parsed = custom.parse(text)
    assert parsed.image == "registry.example.com/sglang:v0.5.19"
    assert parsed.image_tag == "v0.5.19"
    assert parsed.model_path == "/mnt/models/kimi"
    assert parsed.gpus == 4
    assert parsed.env == {"NCCL_DEBUG": "WARN"}
    assert parsed.engine_args == {
        "tp": "4",
        "mem_fraction_static": "0.85",
        "enable_cache_report": True,
    }
    target = LaunchConfig(
        engine_args={
            "tp": 4,
            "mem_fraction_static": 0.9,
            "enable_cache_report": True,
            "attention_backend": "fa3",
        }
    )
    plan = _plan(parsed, target, chart_owned=chart_owned_of(custom))
    after = custom.apply(text, parsed, plan)
    assert '    mem-fraction-static: "0.9"\n' in after
    assert "    enable-cache-report: true\n    attention-backend: fa3\nother:" in after
    assert custom.parse(after).engine_args["attention_backend"] == "fa3"


# -- applying ------------------------------------------------------------------


def test_apply_rewrites_only_the_named_lines():
    head = parse_model_config(H100)
    winner = _winner_h100(
        mem_fraction_static=0.9, attention_backend="flashinfer", enable_cache_report=False, tp=4
    )
    plan = plan_changes(head, winner)
    after = apply_changes(H100, head, plan)

    assert '  - "--mem-fraction-static=0.9"\n' in after
    assert '  - "--mem-fraction-static=0.85"' not in after
    assert '  - "--enable-cache-report"' not in after
    # Appended after the last item, in the block's quoting, before the blank line.
    assert (
        '  - "--tool-call-parser=qwen3_coder"\n  - "--attention-backend=flashinfer"\n\nenv:'
        in after
    )
    assert '  gpus: "4"\n' in after
    # Untouched: comments, the other items, everything else — byte for byte.
    assert after.count("# The shared chart supplies the launcher") == 1
    assert '  - "--tp-size=4"\n' in after
    assert after.splitlines()[:5] == H100.splitlines()[:5]
    assert after.endswith("\n")
    diff = unified_diff(H100, after)
    changed = [
        line
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert sorted(changed) == sorted(
        [
            '-  gpus: "2"',
            '+  gpus: "4"',
            '-  - "--tp-size=2"',
            '+  - "--tp-size=4"',
            '-  - "--mem-fraction-static=0.85"',
            '+  - "--mem-fraction-static=0.9"',
            '-  - "--enable-cache-report"',
            '+  - "--attention-backend=flashinfer"',
        ]
    )
    # The edited file reads back as the winner's config.
    assert parse_model_config(after).engine_args["mem_fraction_static"] == "0.9"
    assert "enable_cache_report" not in parse_model_config(after).engine_args


def test_apply_keeps_bare_items_bare_and_handles_no_trailing_blank():
    head = parse_model_config(B300)
    plan = plan_changes(
        head, dict(head.engine_args, max_running_requests=48, enable_dp_attention=True)
    )
    after = apply_changes(B300, head, plan)
    assert "  - --max-running-requests=48\n" in after
    assert (
        "  - --speculative-draft-model-path=/draft-model\n  - --enable-dp-attention\nenv:\n"
        in after
    )
    assert '"' not in after.split("extraArgs:")[1].split("env:")[0]


def test_apply_removes_a_kept_knob_only_when_the_plan_says_so():
    head = parse_model_config(H100)
    winner = _winner_h100()
    del winner["reasoning_parser"]
    kept = apply_changes(H100, head, plan_changes(head, winner))
    assert kept == H100
    removed = apply_changes(H100, head, plan_changes(head, winner, apply_removals=True))
    assert "--reasoning-parser" not in removed
    assert removed.count("\n") == H100.count("\n") - 1


def test_apply_updates_the_image_tag_in_place():
    head = parse_model_config(H100)
    plan = plan_changes(
        head,
        _winner_h100(),
        winner_image="registry.example.com/sglang:v0.5.19",
        policy=apply_shortcut({}, "follow_image"),
    )
    after = apply_changes(H100, head, plan)
    assert "  tag: v0.5.19\n" in after and "v0.5.15-cu129" not in after


def test_two_line_items_change_on_the_value_line():
    text = B300.replace("  - --tp-size=8\n", '  - --tp-size\n  - "8"\n')
    head = parse_model_config(text)
    assert head.engine_args["tp"] == "8"
    plan = plan_changes(head, dict(head.engine_args, tp=4))
    after = apply_changes(text, head, plan)
    assert '  - --tp-size\n  - "4"\n' in after


def test_apply_refuses_a_file_without_extra_args():
    text = "image:\n  repository: r\n  tag: t\n"
    head = parse_model_config(text)
    plan = plan_changes(head, {"tp": 2})
    with pytest.raises(ValueError):
        apply_changes(text, head, plan)


def test_the_merge_request_branch_prefix_is_configurable():
    """A project may enforce a branch-name push rule, and a rejected push means
    no merge request at all — the deploy repo allows only a fixed set of
    leading words, so `autotune/` is refused there. The prefix is a setting,
    and a marker can be kept inside it."""
    from datetime import UTC, datetime

    from app.control.promotion.merge_request import source_branch_name

    when = datetime(2026, 9, 15, 9, 51, tzinfo=UTC)
    assert source_branch_name("kimi", 1089, when) == "autotune/kimi-run1089-20260915-0951"
    assert (
        source_branch_name("kimi", 1089, when, "feat/autotune-")
        == "feat/autotune-kimi-run1089-20260915-0951"
    )
