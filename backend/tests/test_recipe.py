"""Recipes: the address scheme is lossless, on every variant the catalog has."""

from pathlib import Path

import pytest
import yaml

from app.control.recipe import Recipe, apply, canonical, diff, full_config, shape
from app.control.recipe import args as argv
from app.control.recipe.address import BadAddress, parse, parse_path
from app.control.recipe.check import config_problems, recipe_problems, recipe_warnings
from app.control.recipe.compose import (
    RunPlan,
    Site,
    effective_values,
    layers,
    llmservice,
    mirror_image,
    service_name,
)
from app.control.recipe.legacy import from_fields

VARIANTS = sorted((Path(__file__).parent / "data" / "catalog_variants").glob("*.yaml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _recipe(path: Path) -> Recipe:
    return Recipe.from_variant(_load(path))


@pytest.fixture(scope="module")
def recipes() -> dict[str, Recipe]:
    return {p.stem: _recipe(p) for p in VARIANTS}


def test_the_fixtures_are_there():
    assert len(VARIANTS) >= 70


# -- extraArgs ----------------------------------------------------------------


def test_every_spelling_reads_as_the_same_flags():
    items = [
        "--tp-size=4",
        "--mem-fraction-static 0.85",
        "--chunked-prefill-size",
        "8192",
        "--enable-cache-report",
        '--default-chat-template-kwargs={"thinking": true}',
    ]
    assert argv.flags("sglang", items) == {
        "tp": 4,
        "mem_fraction_static": 0.85,
        "chunked_prefill_size": 8192,
        "enable_cache_report": True,
        "default_chat_template_kwargs": '{"thinking": true}',
    }


def test_the_last_repeat_wins_as_in_argparse():
    assert argv.flags("sglang", ["--tp-size=2", "--tp-size=8"]) == {"tp": 8}


def test_setting_a_flag_rewrites_it_in_place_and_keeps_its_spelling():
    items = ["--a=1", "--tp-size", "2", "--mem-fraction-static 0.8", "--z"]
    out = argv.set_flag("sglang", items, "tp", 4)
    assert out == ["--a=1", "--tp-size", "4", "--mem-fraction-static 0.8", "--z"]
    out = argv.set_flag("sglang", items, "mem_fraction_static", 0.9)
    assert out == ["--a=1", "--tp-size", "2", "--mem-fraction-static 0.9", "--z"]


def test_an_alias_spelling_is_found_and_dropped_duplicates_never_took_effect():
    items = ["--tensor-parallel-size=2", "--x", "--tp-size=4"]
    assert argv.set_flag("sglang", items, "tp", 8) == ["--x", "--tp-size=8"]


def test_a_new_flag_is_appended_and_none_removes():
    assert argv.set_flag("sglang", ["--a=1"], "page_size", 64) == ["--a=1", "--page-size=64"]
    assert argv.set_flag("sglang", ["--page-size=64", "--a=1"], "page_size", None) == ["--a=1"]


def test_false_on_a_switch_removes_it_or_negates_where_the_engine_can():
    assert argv.set_flag("sglang", ["--enable-cache-report"], "enable_cache_report", False) == []
    out = argv.set_flag("vllm", ["--enable-prefix-caching"], "enable_prefix_caching", False)
    assert out == ["--no-enable-prefix-caching"]
    assert argv.flags("vllm", out) == {"enable_prefix_caching": False}


# -- addresses ----------------------------------------------------------------


def test_addresses():
    assert parse("tp").kind == "flag"
    assert parse("env.SGLANG_X").name == "SGLANG_X"
    role = parse("prefill.mem_fraction_static")
    assert (role.kind, role.role, role.name) == ("role_flag", "prefill", "mem_fraction_static")
    assert parse("values.pd.prefill.replicas").path == ("pd", "prefill", "replicas")
    assert parse_path('resources.limits["rdma/hca_shared"]') == (
        "resources",
        "limits",
        "rdma/hca_shared",
    )
    with pytest.raises(BadAddress):
        parse("pd.prefill.replicas")


def test_config_problems_name_the_bad_keys():
    problems = config_problems(
        {
            "values.extraArgs": [],
            "values.nodeSelector.a": "b",
            "values.image.tag": "x",
            "pd.prefill.replicas": 2,
            "tp": 2,
            "values.pd.prefill.replicas": 2,
        }
    )
    assert {p["param"] for p in problems} == {
        "values.extraArgs",
        "values.nodeSelector.a",
        "values.image.tag",
        "pd.prefill.replicas",
    }


# -- lossless, on every catalog variant ---------------------------------------


@pytest.mark.parametrize("path", VARIANTS, ids=lambda p: p.stem)
def test_a_variant_round_trips_through_a_recipe(path):
    variant = _load(path)
    back = Recipe.from_variant(variant).to_variant()
    for key in ("engine", "chart", "image", "values"):
        assert back.get(key) == variant.get(key), key
    assert back["requires"]["gpus"] == variant["requires"]["gpus"]
    assert back["requires"]["topology"] == variant["requires"].get("topology", "single-node")
    if variant.get("servedName"):
        assert back["servedName"] == variant["servedName"]


@pytest.mark.parametrize("path", VARIANTS, ids=lambda p: p.stem)
def test_nothing_differs_from_itself(path):
    recipe = _recipe(path)
    assert diff(recipe, recipe) == {}


def _pairs():
    by_engine: dict[str, list[Path]] = {}
    for path in VARIANTS:
        by_engine.setdefault(_load(path)["engine"], []).append(path)
    out = []
    for paths in by_engine.values():
        for a, b in zip(paths, paths[1:] + paths[:1], strict=True):
            out.append((a, b))
    return out


@pytest.mark.parametrize("a,b", _pairs(), ids=lambda p: p.stem)
def test_diff_then_apply_is_the_target(a, b):
    base, target = _recipe(a), _recipe(b)
    config = diff(base, target)
    assert apply(base, config).hash() == target.hash()
    assert canonical(base, config) == config


@pytest.mark.parametrize("path", VARIANTS, ids=lambda p: p.stem)
def test_one_knob_touches_one_item(path):
    recipe = _recipe(path)
    before = list(recipe.values.get("extraArgs") or [])
    after = apply(recipe, {"mem_fraction_static": 0.5}).values["extraArgs"]
    changed = [i for i, (x, y) in enumerate(zip(before, after, strict=False)) if x != y]
    assert len(changed) <= 1 and len(after) - len(before) in (0, 1)
    assert apply(recipe, {"mem_fraction_static": 0.5}).flags()["mem_fraction_static"] == 0.5


def test_canonical_drops_what_does_not_land():
    base = _recipe(VARIANTS[0])
    flags = base.flags()
    key, value = next(iter(flags.items()))
    assert canonical(base, {key: value, "tensor_parallel_size": base.flags().get("tp", 1)}) == {}


def test_role_flags_env_and_values_round_trip():
    pd = next(p for p in VARIANTS if "pd" in p.stem and "sglang" in p.stem)
    base = _recipe(pd)
    config = {
        "prefill.mem_fraction_static": 0.7,
        "decode.max_running_requests": 512,
        "env.SGLANG_X": "1",
        "values.pd.prefill.replicas": 3,
        "values.localKVOffload.enabled": True,
    }
    out = apply(base, config)
    assert diff(base, out) == config
    assert out.flags("prefill")["mem_fraction_static"] == 0.7
    assert out.role("prefill")["replicas"] == 3


def test_an_env_entry_from_a_secret_survives():
    base = Recipe(values={"env": [{"name": "A", "value": "1"}]})
    target = Recipe(
        values={"env": [{"name": "A", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]}
    )
    assert apply(base, diff(base, target)).values == target.values


def test_full_config_is_flags_role_flags_and_env():
    recipe = Recipe(
        values={
            "extraArgs": ["--tp-size=2"],
            "env": [{"name": "X", "value": "1"}],
            "pd": {"prefill": {"extraArgs": ["--page-size=64"]}},
        }
    )
    assert full_config(recipe) == {"tp": 2, "prefill.page_size": 64, "env.X": "1"}


# -- shape and checks -----------------------------------------------------------


def test_shapes(recipes):
    single = next(r for k, r in recipes.items() if r.topology == "single-node")
    assert shape(single).total_gpus == single.gpus
    lws = next(r for r in recipes.values() if r.topology == "lws")
    size = int(lws.values["lws"]["size"])
    assert shape(lws).total_gpus == lws.gpus * size and shape(lws).pods == size
    pd = next(
        r for r in recipes.values() if r.topology == "pd" and not r.values["pd"].get("colocate")
    )
    roles = {g.role: g for g in shape(pd).groups}
    assert roles["router"].gpus == 0
    assert roles["prefill"].count == int(pd.role("prefill").get("replicas", 1)) * int(
        pd.role("prefill").get("size", 1)
    )


@pytest.mark.parametrize("path", VARIANTS, ids=lambda p: p.stem)
def test_every_published_variant_passes_the_checks(path):
    assert recipe_problems(_recipe(path)) == []


def test_the_checks_catch_what_the_chart_or_the_catalog_would():
    recipe = Recipe(
        values={
            "image": {"repository": "lmsysorg/sglang", "tag": ""},
            "model": {"gpus": "2", "localPath": "/data/x"},
            "extraArgs": ["--tp-size=4", "--port=9", "--context-length=4096"],
            "nodeSelector": {"a": "b"},
            "pd": {"enabled": True},
            "lws": {"enabled": True},
        }
    )
    kinds = sorted((p["kind"], p["param"]) for p in recipe_problems(recipe))
    params = {param for _, param in kinds}
    assert {"values.image", "port", "values.lws.enabled"} <= params
    warnings = " ".join(recipe_warnings(recipe))
    assert "values.nodeSelector" in warnings and "context_length" in warnings
    warm = Recipe(values={"hostNetwork": True, "service": {"port": 30000}})
    assert recipe_warnings(warm)


# -- legacy fields and composition ----------------------------------------------


def test_campaign_fields_become_a_recipe():
    recipe = from_fields(
        engine="sglang",
        image="lmsysorg/sglang:v0.5.20@sha256:" + "a" * 64,
        served_model_name="qwen",
        engine_args={"tp": 4, "context_length": 32768, "port": 1, "env.SGLANG_X": "1"},
        extra_env={"A": "b"},
        extra_volumes={"/data/draft": "/draft:ro"},
    )
    assert recipe.image.startswith("lmsysorg/sglang:v0.5.20@sha256:")
    assert recipe.gpus == 4 and recipe.served_name == "qwen"
    assert recipe.values["model"]["contextLength"] == "32768"
    assert recipe.flags() == {"tp": 4}
    assert recipe.env()["SGLANG_X"] == "1" and recipe.env()["A"] == "b"
    assert recipe.values["volumeMounts"][0] == {
        "name": "extra-0",
        "mountPath": "/draft",
        "readOnly": True,
    }
    assert recipe_problems(recipe) == []
    gang = from_fields(
        engine="sglang", image="i:t", served_model_name="m", engine_args={"tp": 8, "pp": 2}, nodes=2
    )
    assert gang.topology == "lws" and gang.gpus == 8


def test_a_run_layer_wins_over_production_settings():
    recipe = _recipe(next(p for p in VARIANTS if "qwen3.6" in p.stem))
    site = Site(
        namespace="ns",
        chart_repo="https://charts.example",
        model_local_path="/w",
        image_mirror="registry.example/team",
    )
    run = RunPlan(run_id=7, release="autotune-run-7", allowed_nodes=["n1"], startup_minutes=30)
    obj = llmservice(recipe, site, run, chart_version="0.9.26")
    assert obj["spec"]["chart"] == {
        "name": "sglang",
        "repo": "https://charts.example",
        "version": "0.9.26",
    }
    assert [layer["name"] for layer in obj["spec"]["layers"]] == ["catalog", "site", "autotune"]
    values = effective_values(layers(recipe, site, run))
    assert values["terminationGracePeriodSeconds"] == 60
    assert values["lifecycle"]["preStop"]["drainSeconds"] == 5
    assert values["service"]["type"] == "ClusterIP"
    assert values["modelRoute"]["enabled"] is False and values["scaler"]["enabled"] is False
    assert values["model"]["localPath"] == "/w"
    assert values["image"]["repository"].startswith("registry.example/team/")
    assert values["startupProbe"]["failureThreshold"] == 60
    assert values["podLabels"]["autotune.modelsphere.dev/run"] == "7"
    # The catalog layer is the recipe exactly, so its hash is the recipe's.
    assert (
        Recipe(engine="sglang", values=obj["spec"]["layers"][0]["values"]).hash() == recipe.hash()
    )


def test_a_multi_node_run_keeps_a_crashed_pod_and_its_log():
    """The chart recreates an lws group when one pod restarts, deleting the
    pod that crashed: a 2-node run failed with only the leader's "peer went
    away". A run's group restarts in place instead."""
    site = Site(namespace="ns", chart_repo="oci://r")
    gang = from_fields(
        engine="sglang", image="i:t", served_model_name="m", engine_args={"tp": 8, "pp": 2}, nodes=2
    )
    values = effective_values(layers(gang, site, RunPlan(run_id=1, release="r")))
    # The chart's values schema allows Default or RecreateGroupOnPodRestart.
    assert values["lws"]["restartPolicy"] == "Default"
    assert values["lws"]["enabled"] is True and values["lws"]["size"] == 2
    single = Recipe(values={"image": {"repository": "a", "tag": "b"}})
    plain = effective_values(layers(single, site, RunPlan(run_id=1, release="r")))
    assert "restartPolicy" not in (plain.get("lws") or {})


def test_nodeport_is_opt_in():
    recipe = Recipe(values={"image": {"repository": "a", "tag": "b"}})
    site = Site(namespace="ns", chart_repo="oci://r", endpoint="nodeport")
    values = effective_values(layers(recipe, site, RunPlan(run_id=1, release="r")))
    assert values["service"]["type"] == "NodePort"


def test_mirror_and_service_names():
    assert mirror_image("lmsysorg/sglang", "reg.example/x") == "reg.example/x/sglang"
    assert mirror_image("ghcr.io/a/b", "reg.example/x") == "reg.example/x/b"
    assert mirror_image("reg.example/x/a", "reg.example/x") == "reg.example/x/a"
    lws = Recipe(values={"lws": {"enabled": True}})
    assert service_name(lws, "autotune-run-3") == "autotune-run-3-leader"
    assert service_name(Recipe(values={"pd": {"enabled": True}}), "r") == "r"
