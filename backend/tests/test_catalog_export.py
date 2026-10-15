"""model-catalog export: measured configs become catalog.swiss/v2 variant files
and a metadata.yaml that the catalog's own schemas and rules accept, carrying
nothing site-owned, and building on what the catalog already publishes."""

import io
import json
import zipfile
from itertools import count

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.control.catalog_export import (
    CatalogModel,
    CatalogOptions,
    VariantSource,
    build_catalog_entry,
    extra_args,
    guess_hf,
    split_image,
    validate,
    variant_problems,
)
from app.control.catalog_export.catalog_index import lookup
from app.control.catalog_export.validation import content_sha256, validation_file
from app.control.launch_config import LaunchConfig
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, Candidate, Result, Run, User
from app.main import app

_counter = count()
HF = "Qwen/Qwen3.5-35B-A3B"
REPORT = "q-h100-report.html"


def _config(**overrides) -> LaunchConfig:
    base = dict(
        engine="sglang",
        image="registry.example.com:5000/team/sglang:v0.5.15-cu129",
        model_path="/models/Qwen/Qwen3.5-35B-A3B",
        served_model_name="qwen3.5",
        engine_args={
            "tp": 2,
            "mem_fraction_static": 0.83,
            "enable_cache_report": True,
            "host": "0.0.0.0",
            "port": 28200,
        },
        extra_env={"PYTORCH_ALLOC_CONF": "expandable_segments:True", "CUDA_VISIBLE_DEVICES": "0,1"},
        extra_volumes={"/root/moe-config": "/moe-config:ro"},
        gpu_type="H100",
    )
    return LaunchConfig(**{**base, **overrides}).normalized()


def _pair() -> list[VariantSource]:
    baseline = _config()
    tuned = _config(
        engine_args={**baseline.engine_args, "mem_fraction_static": 0.88, "context_length": 131072}
    )
    return [
        VariantSource(
            baseline,
            role="baseline",
            evidence={"metric": "tps", "value": 100.0, "benchmark": "b82"},
        ),
        VariantSource(
            tuned, role="optimized", evidence={"metric": "tps", "value": 125.0, "benchmark": "b82"}
        ),
    ]


def _opts(**kw) -> CatalogOptions:
    """A model new to the catalog, with what that needs: its repo id, and the
    report a tuning result points at."""
    return CatalogOptions(**{"name": "q", "hf": HF, "report_file": REPORT, **kw})


def _docs(out):
    return [v.document for v in out.variants]


def _text(out, path):
    return next(f.text for f in out.files if f.path == path)


# -- the files ------------------------------------------------------------------


def test_a_baseline_and_its_optimized_sibling_are_two_variant_files():
    out = build_catalog_entry(_pair(), _opts(name="qwen3.5-35b-a3b"))
    assert out.errors == [], out.errors
    assert [f.path for f in out.files] == [
        "models/qwen3.5-35b-a3b/sglang-tp2-h100-baseline.yaml",
        "models/qwen3.5-35b-a3b/sglang-tp2-h100-optimized.yaml",
        "models/qwen3.5-35b-a3b/metadata.yaml",
    ]
    base, tuned = _docs(out)
    assert yaml.safe_load(_text(out, out.variants[1].path)) == tuned, "the file is the document"
    assert {k: base[k] for k in ("apiVersion", "kind", "model", "id", "version")} == {
        "apiVersion": "catalog.swiss/v2",
        "kind": "Variant",
        "model": "qwen3.5-35b-a3b",
        "id": "sglang-tp2-h100-baseline",
        "version": "1.0.0",
    }
    assert base["servedName"] == "qwen3.5"
    assert base["chart"] == {"name": "sglang", "version": ">=0.8.0"}, "the catalog's minimum"
    assert tuned["description"] == (
        "Tuned by LLM AutoTune: TP2 on two H100 GPUs, single node."
        " +25% on the tuning benchmark vs the baseline variant."
    )
    assert base["image"] == {
        "repository": "registry.example.com:5000/team/sglang",
        "tag": "v0.5.15-cu129",
    }
    assert base["requires"] == {
        "gpus": 2,
        "topology": "single-node",
        "gpuProduct": ["NVIDIA-H100-80GB-HBM3"],
    }
    assert [(v.status, v.role) for v in out.variants] == [("new", "baseline"), ("new", "optimized")]


def test_the_metadata_records_the_tuning_result_and_preselects_it():
    pair = _pair()
    pair[1].evidence["scenarios"] = [
        {"key": "a", "label": "50k + 1.5k", "pct": 25.04},
        {"key": "b", "label": "8k + 1k", "pct": 9.95},
    ]
    out = build_catalog_entry(pair, _opts())
    meta = out.metadata
    assert meta["apiVersion"] == "catalog.swiss/v2" and meta["source"] == {"hf": HF}
    assert meta["defaultVariant"] == "sglang-tp2-h100-optimized"
    assert meta["tuning"] == [
        {
            "baseline": "sglang-tp2-h100-baseline@1.0.0",
            "optimized": "sglang-tp2-h100-optimized@1.0.0",
            "uplift": 25.0,
            "workloads": [
                {"name": "50k + 1.5k", "uplift": 25.0},
                {"name": "8k + 1k", "uplift": 10.0},
            ],
            "report": REPORT,
        }
    ]
    assert validate(meta, "metadata.v2") == []
    assert list(meta)[:2] == ["apiVersion", "name"] and list(meta)[-1] == "source"


def test_a_tuned_variant_exports_without_its_report_and_says_what_is_missing():
    no_report = build_catalog_entry(_pair(), _opts(report_file=""))
    assert no_report.errors == [], "a missing report never blocks the export"
    assert "tuning" not in no_report.metadata
    assert no_report.variants[1].tuning == "no_report" and no_report.variants[1].uplift == 25.0
    assert no_report.warnings == [], "the variant's own status says it; nothing to read twice"
    alone = build_catalog_entry(_pair()[1:], _opts())
    assert alone.errors == []
    assert alone.variants[0].tuning == "no_baseline"
    named = build_catalog_entry(
        [VariantSource(_config(), id="sglang-tp2-h100-optimized")], _opts(report_file="")
    )
    assert any("the id says optimized" in w for w in named.warnings)


def test_nothing_site_owned_or_chart_owned_is_published():
    out = build_catalog_entry(_pair(), _opts())
    base, tuned = _docs(out)
    values = base["values"]
    assert values["extraArgs"] == [
        "--enable-cache-report",
        "--mem-fraction-static=0.83",
        "--stream-response-default-include-usage",
        "--tp-size=2",
    ], "host/port are the chart's; the serving contract is always there"
    assert "localPath" not in values["model"]
    assert values["model"]["contextLength"] == "", "never pinned unless the config pinned it"
    assert tuned["values"]["model"]["contextLength"] == "131072"
    assert not any("context-length" in a for a in tuned["values"]["extraArgs"])
    assert values["env"] == [
        {"name": "NVIDIA_DISABLE_REQUIRE", "value": "1"},
        {"name": "PYTORCH_ALLOC_CONF", "value": "expandable_segments:True"},
    ], "the driver's defaults ride along; CUDA_VISIBLE_DEVICES is the platform's"
    assert [v["name"] for v in values["volumes"]] == ["dot-cache", "dshm"], "no host paths"
    moe = next(i for i in out.variants[0].inputs if i.name == "/moe-config")
    assert (moe.category, moe.action) == ("site", "left out"), "nothing reads it"
    assert moe.reason == "unused" and not any("moe-config" in w for w in out.warnings)
    assert not any("/models/" in f.text for f in out.files)


def test_a_config_without_extra_env_still_exports_the_env_it_ran_with():
    out = build_catalog_entry(
        [VariantSource(_config(extra_env={}), role="baseline")], _opts()
    )
    assert _docs(out)[0]["values"]["env"] == [
        {"name": "NVIDIA_DISABLE_REQUIRE", "value": "1"},
        {"name": "PYTORCH_ALLOC_CONF", "value": "expandable_segments:True"},
    ]
    own = build_catalog_entry(
        [
            VariantSource(
                _config(extra_env={"PYTORCH_ALLOC_CONF": "x", "SGLANG_FOO": "1"}), role="baseline"
            )
        ],
        _opts(),
    )
    assert _docs(own)[0]["values"]["env"] == [
        {"name": "NVIDIA_DISABLE_REQUIRE", "value": "1"},
        {"name": "PYTORCH_ALLOC_CONF", "value": "x"},
        {"name": "SGLANG_FOO", "value": "1"},
    ], "the config's own value wins over the default"


def test_a_draft_model_a_flag_reads_is_kept_where_sites_keep_weights():
    cfg = _config(
        engine_args={
            "tp": 2,
            "speculative_algorithm": "EAGLE3",
            "speculative_draft_model_path": "/spec-model",
        },
        extra_volumes={
            "/mnt/disk0/models/EAGLE3-x": "/spec-model",
            "/root/moe-config": "/moe-config:ro",
        },
    )
    out = build_catalog_entry([VariantSource(cfg, role="baseline")], _opts())
    assert out.errors == []
    values = _docs(out)[0]["values"]
    assert values["volumes"][0] == {
        "name": "draft-model",
        "hostPath": {"path": "/data/models/EAGLE3-x", "type": "Directory"},
    }, "not this site's disk: where every site keeps weights"
    assert values["volumeMounts"][0] == {
        "name": "draft-model",
        "mountPath": "/spec-model",
        "readOnly": True,
    }
    assert [v["name"] for v in values["volumes"]] == ["draft-model", "dot-cache", "dshm"]
    draft = next(i for i in out.variants[0].inputs if i.name == "/spec-model")
    assert (draft.category, draft.action) == ("model", "moved")
    assert draft.reason == "draft model, at /data/models/EAGLE3-x"


def _inputs(out, kind="env"):
    return {i.name: (i.category, i.action) for i in out.variants[0].inputs if i.kind == kind}


def test_env_is_split_into_the_recipe_and_this_sites_setup():
    env = {
        "SGLANG_ENABLE_JIT_DEEPGEMM": "0",
        "NCCL_SOCKET_IFNAME": "bond0",
        "MOONCAKE_MASTER": "etcd://198.51.100.13:2379",
        "LD_LIBRARY_PATH": "/usr/local/nvidia/lib64",
        "MY_ENDPOINT": "http://store.example:8080",
        "HF_TOKEN": "hf_x",
        "CUDA_VISIBLE_DEVICES": "0,1",
        "SOMETHING_NEW": "1",
    }
    out = build_catalog_entry([VariantSource(_config(extra_env=env))], _opts())
    got = _inputs(out)
    assert got["SGLANG_ENABLE_JIT_DEEPGEMM"] == ("recipe", "kept")
    assert got["PYTORCH_ALLOC_CONF"] == ("recipe", "kept"), "the driver's default it ran with"
    for site in ("NCCL_SOCKET_IFNAME", "MOONCAKE_MASTER", "LD_LIBRARY_PATH", "MY_ENDPOINT"):
        assert got[site] == ("site", "left out"), site
    assert got["HF_TOKEN"] == ("secret", "left out")
    assert got["CUDA_VISIBLE_DEVICES"] == ("platform", "left out")
    assert got["SOMETHING_NEW"] == ("unknown", "kept")
    names = [e["name"] for e in _docs(out)[0]["values"]["env"]]
    assert names == ["NVIDIA_DISABLE_REQUIRE", "PYTORCH_ALLOC_CONF",
                     "SGLANG_ENABLE_JIT_DEEPGEMM", "SOMETHING_NEW"]
    assert out.warnings == [], "env decisions live in the inputs, not in warnings"

    settled = build_catalog_entry(
        [VariantSource(_config(extra_env=env))],
        _opts(env_categories={"SOMETHING_NEW": "site", "NCCL_SOCKET_IFNAME": "recipe"}),
    )
    got = _inputs(settled)
    assert got["SOMETHING_NEW"] == ("site", "left out") and got["NCCL_SOCKET_IFNAME"] == (
        "recipe", "kept"), "a person's call wins over the rules"


def test_a_mount_an_env_var_reads_is_kept_only_with_that_env_var():
    cfg = _config(
        extra_env={"SGLANG_CONFIG": "/cfg/engine.json",
                   "MOONCAKE_CONFIG_PATH": "/root/mc-config/mooncake.json"},
        extra_volumes={"/etc/sglang": "/cfg:ro", "/etc/mooncake": "/root/mc-config"},
    )
    out = build_catalog_entry([VariantSource(cfg)], _opts())
    mounts = _inputs(out, "mount")
    assert mounts["/cfg"] == ("config", "kept"), "the recipe's env var reads it"
    assert mounts["/root/mc-config"] == ("site", "left out"), "only a site env var reads it"
    env = _inputs(out)
    assert env["SGLANG_CONFIG"] == ("recipe", "kept"), "its path is one the entry provides"
    assert env["MOONCAKE_CONFIG_PATH"] == ("site", "left out")
    values = _docs(out)[0]["values"]
    assert {"name": "cfg", "mountPath": "/cfg", "readOnly": True} in values["volumeMounts"]


def test_a_cache_mount_becomes_an_empty_directory():
    cfg = _config(extra_volumes={"/data/triton-cache": "/root/.triton",
                                 "/data/hf": "/root/.cache/huggingface"})
    out = build_catalog_entry([VariantSource(cfg)], _opts())
    mounts = _inputs(out, "mount")
    assert mounts["/root/.triton"] == ("scratch", "emptyDir")
    assert mounts["/root/.cache/huggingface"] == ("scratch", "emptyDir")
    values = _docs(out)[0]["values"]
    triton = next(v for v in values["volumes"] if v["name"] == "triton")
    assert triton == {"name": "triton", "emptyDir": {"sizeLimit": "10Gi"}}
    assert not any("hostPath" in v for v in values["volumes"]), "no host path published"
    assert out.errors == []


def test_the_serving_contract_is_added_and_a_weights_file_pins_the_mount():
    cfg = _config(engine_args={"tp": 8, "chat_template": "/model/chat_template.jinja"})
    out = build_catalog_entry([VariantSource(cfg, role="baseline")], _opts())
    values = _docs(out)[0]["values"]
    assert "--enable-cache-report" in values["extraArgs"]
    assert "--stream-response-default-include-usage" in values["extraArgs"]
    assert any("which every catalog entry serves with" in d for d in out.details)
    assert values["model"]["mountPath"] == "/model", "--chat-template reads from the weights"
    plain = build_catalog_entry(
        [VariantSource(_config(engine_args={"tp": 2, "enable_cache_report": False}))], _opts()
    )
    pv = _docs(plain)[0]["values"]
    assert "mountPath" not in pv["model"]
    assert "--enable-cache-report" not in pv["extraArgs"], "an explicit False is the config's"


def test_volumes_only_on_request():
    out = build_catalog_entry(_pair()[:1], _opts(include_volumes=True))
    values = _docs(out)[0]["values"]
    assert values["volumes"][0]["hostPath"]["path"] == "/root/moe-config"
    assert values["volumeMounts"][0] == {
        "name": "moe-config",
        "mountPath": "/moe-config",
        "readOnly": True,
    }
    assert out.errors == []


def test_multi_node_is_an_lws_group_sized_per_pod():
    cfg = _config(engine_args={"tp": 8, "pp_size": 2})
    out = build_catalog_entry([VariantSource(cfg, nodes=2)], _opts(name="kimi-k2.5"))
    v = _docs(out)[0]
    assert {k: x for k, x in v["requires"].items() if k != "gpuProduct"} == {
        "gpus": 8,
        "nodes": 2,
        "topology": "lws",
        "rdma": True,
    }
    assert v["values"]["resources"]["limits"]["rdma/hca_shared"] == "1"
    assert v["values"]["lws"] == {"enabled": True, "size": 2}
    assert out.errors == []


def test_the_schema_catches_what_the_builder_cannot_fix():
    too_big = build_catalog_entry([VariantSource(_config(engine_args={"tp": 16}))], _opts())
    assert any("requires/gpus" in e for e in too_big.errors), "16 cards on one pod is refused"


def test_small_helpers():
    assert split_image("reg:5000/a/b:t@sha256:" + "a" * 64)[1] == "t"
    assert split_image("reg:5000/a/b") == ("reg:5000/a/b", "", "")
    assert guess_hf("/models/glm-5") == ""
    args, context, dropped = extra_args(
        "sglang", {"disable_radix_cache": False, "max_model_len": 8}
    )
    assert context == "8" and "--disable-radix-cache" not in args


# -- what the catalog cannot be given silently ----------------------------------


def test_a_new_model_needs_its_repo_id_and_it_is_never_guessed_into_the_file():
    out = build_catalog_entry(_pair(), _opts(hf=""))
    assert any(e.startswith("source.hf") and HF in e for e in out.errors), "suggested, not set"
    assert out.hf_suggestion == HF
    assert "source" not in out.metadata
    none = build_catalog_entry(
        [VariantSource(_config(model_path="/data/weights"))], _opts(hf="")
    )
    assert none.hf_suggestion == "" and any("source.hf" in e for e in none.errors)


def test_an_image_named_by_digest_alone_needs_its_tag():
    digest = "sha256:" + "c" * 64
    cfg = _config(image=f"registry.example.com/team/sglang@{digest}")
    out = build_catalog_entry([VariantSource(cfg)], _opts())
    assert any("has no tag" in e for e in out.errors), "never the chart's default image"
    tagged = build_catalog_entry([VariantSource(cfg, image_tag="v0.5.15")], _opts())
    assert tagged.errors == []
    assert _docs(tagged)[0]["image"] == {
        "repository": "registry.example.com/team/sglang",
        "tag": "v0.5.15",
        "digest": digest,
    }


def test_a_flag_path_no_mount_provides_is_an_error():
    # The chart mounts weights at /models unless the variant pins model.mountPath.
    variant = {
        "engine": "sglang",
        "chart": {"name": "sglang"},
        "requires": {"gpus": 8},
        "values": {
            "model": {"hostPathType": "Directory"},
            "extraArgs": ["--chat-template=/model/chat_template.jinja", "--tp-size=8"],
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
        },
    }
    assert any("/model/chat_template.jinja" in e and "no mount" in e
               for e in variant_problems(variant))
    variant["values"]["model"]["mountPath"] = "/model"
    assert variant_problems(variant) == []
    variant["values"]["extraArgs"].append("--speculative-draft-model-path=/spec-model")
    assert variant_problems(variant)
    variant["values"]["volumeMounts"].append({"name": "draft-model", "mountPath": "/spec-model"})
    assert variant_problems(variant) == []
    variant["chart"]["name"] = "vllm"
    assert any("is not its engine" in e for e in variant_problems(variant))


# -- a model the catalog already has --------------------------------------------


def _published(**variants) -> CatalogModel:
    first = build_catalog_entry(_pair(), _opts())
    meta = dict(first.metadata)
    meta["tuning"] = [{"baseline": "old-baseline@1.0.0", "optimized": "old-optimized@1.0.0",
                       "report": "old-report.html"}]
    meta["defaultVariant"] = "old-optimized"
    return CatalogModel(
        name="qwen3.5-35b-a3b",
        metadata=meta,
        metadata_text=yaml.safe_dump(meta),
        variants={
            "old-baseline": {"version": "1.0.0", "document": None},
            "old-optimized": {"version": "1.0.0", "document": None},
            **variants,
        },
    )


def test_a_published_model_keeps_its_name_its_metadata_and_its_default():
    out = build_catalog_entry(_pair(), _opts(name="anything", hf="", existing=_published()))
    assert out.errors == [], out.errors
    assert out.existing and out.name == "qwen3.5-35b-a3b"
    assert any("the catalog has this model as" in d for d in out.details)
    meta = out.metadata
    assert meta["source"] == {"hf": HF}, "no new repo id is asked for"
    assert meta["defaultVariant"] == "old-optimized", "the published default stays"
    assert [t["optimized"] for t in meta["tuning"]] == [
        "old-optimized@1.0.0",
        "sglang-tp2-h100-optimized@1.0.0",
    ], "the new result is added to the recorded ones"


def test_a_republished_variant_is_its_next_version_or_no_file_at_all():
    first = build_catalog_entry(_pair(), _opts(name="qwen3.5-35b-a3b"))
    same = {"version": "1.2.0", "document": first.variants[0].document | {"version": "1.2.0"}}
    changed = {
        "version": "1.0.3",
        "document": first.variants[1].document | {"description": "edited by hand"},
    }
    published = _published(
        **{"sglang-tp2-h100-baseline": same, "sglang-tp2-h100-optimized": changed}
    )
    out = build_catalog_entry(_pair(), _opts(existing=published))
    assert out.errors == [], out.errors
    assert [(v.id, v.status, v.version) for v in out.variants] == [
        ("sglang-tp2-h100-baseline", "unchanged", "1.2.0"),
        ("sglang-tp2-h100-optimized", "updated", "1.1.0"),
    ]
    assert [f.path for f in out.files] == [
        "models/qwen3.5-35b-a3b/sglang-tp2-h100-optimized.yaml",
        "models/qwen3.5-35b-a3b/metadata.yaml",
    ], "an unchanged variant is not rewritten"
    assert out.metadata["tuning"][-1] == {
        "baseline": "sglang-tp2-h100-baseline@1.2.0",
        "optimized": "sglang-tp2-h100-optimized@1.1.0",
        "uplift": 25.0,
        "report": REPORT,
    }, "the result names the versions measured"


def test_a_model_still_in_the_v1_format_is_refused():
    published = _published()
    published.metadata["apiVersion"] = "catalog.swiss/v1"
    out = build_catalog_entry(_pair(), _opts(existing=published))
    assert any("v1 format" in e for e in out.errors)


async def test_the_catalog_is_read_from_its_index():
    index = {
        "apiVersion": "catalog.swiss/v2",
        "raw": "https://raw.example/model-catalog/",
        "commit": "abc",
        "models": [
            {
                "name": "qwen3.5-35b-a3b",
                "source": {"hf": HF},
                "variants": [
                    {"id": "v1", "version": "1.0.4", "path": "models/qwen3.5-35b-a3b/v1.yaml"}
                ],
            }
        ],
    }
    files = {
        "/model-catalog/abc/models/qwen3.5-35b-a3b/metadata.yaml":
            f"apiVersion: catalog.swiss/v2\nname: qwen3.5-35b-a3b\nsource:\n  hf: {HF}\n",
        "/model-catalog/abc/models/qwen3.5-35b-a3b/v1.yaml": "id: v1\nversion: 1.0.4\n",
    }

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index-v2.json":
            return httpx.Response(200, json=index)
        if request.url.path in files:
            return httpx.Response(200, text=files[request.url.path])
        return httpx.Response(404)

    transport = httpx.MockTransport(serve)
    found, note = await lookup("https://site.example/index-v2.json", "other-name", HF.lower(),
                               transport=transport)
    assert note == "" and found is not None, "found by its repo id, whatever the name"
    assert found.name == "qwen3.5-35b-a3b" and found.metadata["source"]["hf"] == HF
    assert found.variants == {"v1": {"version": "1.0.4", "document": {"id": "v1",
                                                                      "version": "1.0.4"}}}
    missing, _ = await lookup("https://site.example/index-v2.json", "new-model", "a/b",
                              transport=transport)
    assert missing is None

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    offline, note = await lookup("https://site.example/index-v2.json", "q", HF,
                                 transport=httpx.MockTransport(down))
    assert offline is None and "could not read the catalog's index" in note
    assert await lookup("", "q", HF) == (None, "")


# -- the CI routing file -------------------------------------------------------


def test_the_routing_file_names_each_variant_by_content_not_bytes():
    sources = _pair()
    for i, source in enumerate(sources):
        source.origin = {"track_slug": f"track-{i}", "run_id": 100 + i}
    out = build_catalog_entry(sources, _opts())
    sidecar = validation_file(out, sources)
    doc = json.loads(sidecar.text)
    assert sidecar.path == "models/q/q.validation.json" and doc["schema_version"] == 2
    assert [(v["variant_id"], v["version"], v["role"], v["file"]) for v in doc["variants"]] == [
        ("sglang-tp2-h100-baseline", "1.0.0", "baseline", "sglang-tp2-h100-baseline.yaml"),
        ("sglang-tp2-h100-optimized", "1.0.0", "optimized", "sglang-tp2-h100-optimized.yaml"),
    ]
    text = out.files[0].text
    reformatted = yaml.safe_dump(yaml.safe_load(text), indent=4, sort_keys=True)
    assert doc["variants"][0]["content_sha256"] == content_sha256(reformatted)
    # Only an explicit role is routed as optimized.
    sources[1].role = ""
    plain = build_catalog_entry(sources, _opts())
    assert json.loads(validation_file(plain, sources).text)["variants"][1]["role"] == "other"


def test_no_routing_file_without_a_track_for_every_run_or_with_errors():
    sources = _pair()
    sources[0].origin = {"track_slug": "t", "run_id": 100}
    out = build_catalog_entry(sources, _opts())
    assert validation_file(out, sources) is None
    assert any("not every run belongs to a track" in d for d in out.details)
    out.errors.append("invalid")
    assert validation_file(out, sources) is None


# -- drafted text -----------------------------------------------------------------


def test_metadata_is_drafted_from_the_model_and_the_configs():
    out = build_catalog_entry(
        _pair(), _opts(name="qwen3.5-35b-a3b", model_label="Qwen3.5-35B-A3B")
    )
    meta = out.metadata
    assert meta["displayName"] == "Qwen3.5-35B-A3B"
    assert meta["family"] == "qwen"
    assert meta["tags"] == ["chat", "moe"], "A3B reads as MoE; no parser, no reasoning tag"
    assert "variant" not in meta["description"], "metadata outlives any one variant"
    assert meta["description"].startswith("Qwen3.5-35B-A3B on 2 H100 GPUs")
    given = build_catalog_entry(
        _pair(),
        _opts(model_label="Qwen3.5", display_name="Mine", tags=["x"],
              description="hand-written", homepage="https://example.com"),
    )
    assert given.metadata["displayName"] == "Mine"
    assert given.metadata["tags"] == ["x"]
    assert given.metadata["description"] == "hand-written"
    assert given.metadata["homepage"] == "https://example.com"
    assert given.errors == []


def test_the_values_follow_the_catalogs_newest_entries():
    out = build_catalog_entry(_pair()[:1], _opts())
    values = _docs(out)[0]["values"]
    assert values["modelCheck"]["enabled"] is True
    assert values["resources"]["limits"]["ephemeral-storage"] == "16Gi"
    assert values["lifecycle"]["preStop"]["drainSeconds"] == 600
    assert values["terminationGracePeriodSeconds"] == 3600
    assert values["hangWatcher"] == {"enabled": True}
    probe = values["startupProbe"]
    assert probe["periodSeconds"] * probe["failureThreshold"] == 90 * 60, "the 90-minute floor"
    assert out.files[0].text.startswith("# Serving configs measured"), "a short header"
    assert "run " not in out.files[0].text
    assert out.errors == []


def test_a_measured_load_only_raises_the_startup_budget():
    slow = VariantSource(_config(), load_seconds=45 * 60)  # 3 x 45 = 135 minutes
    fast = VariantSource(_config(), load_seconds=5 * 60)
    out = build_catalog_entry([slow, fast], _opts())
    a, b = (v["values"]["startupProbe"] for v in _docs(out))
    assert a["failureThreshold"] * 30 == 135 * 60
    assert b["failureThreshold"] * 30 == 90 * 60


def test_unknown_cards_and_dev_tags_are_flagged_not_guessed():
    cfg = _config(gpu_type="H20", image="registry.example.com/team/sglang:dev-dsv41")
    out = build_catalog_entry([VariantSource(cfg)], _opts())
    v = _docs(out)[0]
    assert "gpuProduct" not in v["requires"]
    assert any("no node label known for H20" in w for w in out.warnings)
    assert any("isn't a release tag and may move" in w for w in out.warnings)
    pinned = build_catalog_entry(
        [VariantSource(cfg, image_digest="sha256:" + "b" * 64)],
        _opts(gpu_product=["NVIDIA-H20"]),
    )
    v = _docs(pinned)[0]
    assert v["image"]["digest"] == "sha256:" + "b" * 64
    assert v["requires"]["gpuProduct"] == ["NVIDIA-H20"]
    assert pinned.errors == []


def test_speculative_and_quantized_configs_are_described_and_tagged():
    cfg = _config(
        engine_args={
            "tp": 8,
            "speculative_algorithm": "eagle",
            "quantization": "fp8",
            "kv_cache_dtype": "fp8_e4m3",
        }
    )
    out = build_catalog_entry([VariantSource(cfg)], _opts(model_label="MiMo-V2.5"))
    desc = _docs(out)[0]["description"]
    assert "fp8 weights, fp8 KV cache, EAGLE speculative decoding" in desc
    assert "speculative-decoding" in out.metadata["tags"] and "fp8" in out.metadata["tags"]


def _judged_pair(scenarios, quality):
    pair = _pair()
    pair[1].evidence = {**pair[1].evidence, "scenarios": scenarios, "quality": quality}
    return pair


def test_the_description_carries_every_scenario_and_losses_are_flagged():
    pair = _judged_pair(
        [
            {"key": "a", "label": "50k + 1.5k", "pct": 89.49},
            {"key": "b", "label": "8k + 1k", "pct": -4.2},
        ],
        [{"name": "GPQA Diamond", "baseline": 0.9, "attempt": 0.8384}],
    )
    out = build_catalog_entry(pair, _opts())
    tuned = _docs(out)[1]
    assert "+89.5% (50k + 1.5k), -4.2% (8k + 1k)" in tuned["description"]
    assert "on the tuning benchmark" not in tuned["description"], "not the single ranking metric"
    assert any("does NOT beat the baseline on 8k + 1k" in w for w in out.warnings)
    assert any(
        "answer quality drops on GPQA Diamond" in w and "-6.16 points" in w for w in out.warnings
    )
    assert out.metadata["tuning"][0]["workloads"][1] == {"name": "8k + 1k", "uplift": -4.2}
    assert out.errors == [], "a reviewer decides; the export still validates"


def test_a_winning_pair_raises_no_judgement():
    pair = _judged_pair(
        [
            {"key": "a", "label": "50k + 1.5k", "pct": 5.5},
            {"key": "b", "label": "8k + 1k", "pct": 8.1},
        ],
        [{"name": "GPQA Diamond", "baseline": 0.8182, "attempt": 0.8485}],
    )
    out = build_catalog_entry(pair, _opts())
    assert not any("does NOT beat" in w or "quality drops" in w for w in out.warnings)


def test_changes_round_the_way_the_report_prints_them():
    from app.control.catalog_export import pct_text, round_pct

    assert pct_text(119.85) == "+119.9%", "half up, like the report; not Python's +119.8%"
    assert pct_text(9.99) == "+10.0%" and pct_text(-4.25) == "-4.3%" and pct_text(0.04) == "0.0%"
    assert round_pct(119.85) == 119.9 and round_pct(-4.25) == -4.3


# -- the endpoint ---------------------------------------------------------------


@pytest_asyncio.fixture
async def stack():
    name = f"ce_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        session.add(User(id=1, username="admin", password_hash="x", role="admin"))
        await session.commit()
        token = create_token(await session.get(User, 1))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        yield http, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(factory):
    """A campaign with a baseline run and a tuned run, both measured, and a
    third run that never got a result."""
    async with factory() as s:
        s.add(
            Campaign(
                id=9,
                owner_id=1,
                name="qwen3.5 h800",
                engine="sglang",
                image="registry.example.com/team/sglang:v0.5.15",
                model_path="/models/Qwen/Qwen3.5-35B-A3B",
                served_model_name="Qwen3.5-35B-A3B",
                search_space={"grid": {"mem_fraction_static": [0.83, 0.88, 0.9]}},
                objective={"target_metric": "request_output_tps", "direction": "maximize"},
                benchmark_slug="bench-82",
            )
        )
        for run_id, mem, value in ((101, 0.83, 1000.0), (102, 0.88, 1200.0), (103, 0.9, None)):
            s.add(
                Candidate(
                    id=run_id,
                    campaign_id=9,
                    config_hash=f"h{run_id}",
                    config={"tp": 2, "mem_fraction_static": mem},
                )
            )
            s.add(
                Run(
                    id=run_id,
                    campaign_id=9,
                    candidate_id=run_id,
                    env_snapshot={"card_type": "H800"},
                )
            )
            if value is not None:
                s.add(
                    Result(
                        run_id=run_id,
                        source="llmbench",
                        objective_value=value,
                        feasible=True,
                        metrics={"request_output_tps": value},
                    )
                )
        await s.commit()


PAIR = [{"run_id": 101, "role": "baseline"}, {"run_id": 102, "role": "optimized", "label": "tuned"}]


async def test_preview_exports_a_baseline_run_and_a_tuned_run(stack):
    http, factory = stack
    await _seed(factory)
    res = await http.post(
        "/api/catalog-export/preview",
        json={"variants": PAIR, "hf": HF, "use_catalog": False},
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["name"] == "qwen3.5-35b-a3b", "named after the served model"
    assert [v["id"] for v in out["variants"]] == [
        "sglang-tp2-h800-baseline",
        "sglang-tp2-h800-optimized",
    ]
    assert [f["kind"] for f in out["files"]] == ["variant", "variant", "metadata"]
    assert out["errors"] == [] and out["report_source"] == ""
    assert out["variants"][1]["tuning"] == "no_report" and out["warnings"] == []
    assert out["hf"] == HF
    assert "campaign 9, run 101" in out["provenance"][0]
    assert out["provenance"][1].startswith("tuned: ")
    for f in out["files"]:  # the report too: its embedded comparison is scrubbed
        for leak in ("run 101", '"run_id": 101', "qwen3.5 h800", "/models/Qwen", "/api/agent"):
            assert leak not in f["text"], (f["path"], leak)
    tuned = yaml.safe_load(out["files"][1]["text"])
    assert "+20% on the tuning benchmark vs the baseline variant" in tuned["description"]


async def test_the_archive_is_the_files_laid_out_as_the_catalog_is(stack):
    http, factory = stack
    await _seed(factory)
    body = {"variants": PAIR[:1], "hf": HF, "use_catalog": False}
    res = await http.post("/api/catalog-export/archive", json=body)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert names == [
        "models/qwen3.5-35b-a3b/sglang-tp2-h800-baseline.yaml",
        "models/qwen3.5-35b-a3b/metadata.yaml",
    ]
    refused = await http.post(
        "/api/catalog-export/archive", json={"variants": PAIR, "use_catalog": False}
    )
    assert refused.status_code == 422 and "source.hf" in refused.text


async def test_preview_exports_validation_from_plugin_track_overlay(stack, monkeypatch):
    from app import plugins
    from app.agent.overlay import RunOverlay
    from app.schemas.agent import RunGroup

    monkeypatch.setattr(plugins, "run_overlay", lambda session, run, campaign: RunOverlay(
        group=RunGroup(kind="track", slug="original-track")
    ))
    http, factory = stack
    await _seed(factory)
    res = await http.post("/api/catalog-export/preview", json={
        "variants": [{"run_id": 101, "role": "baseline", "id": "my-baseline"}],
        "hf": HF, "use_catalog": False,
    })
    assert res.status_code == 200, res.text
    out = res.json()
    sidecar = next(f for f in out["files"] if f["kind"] == "validation")
    manifest = json.loads(sidecar["text"])
    assert sidecar["path"] == "models/qwen3.5-35b-a3b/qwen3.5-35b-a3b.validation.json"
    assert manifest["variants"][0] | {"content_sha256": "-"} == {
        "variant_id": "my-baseline", "version": "1.0.0", "track_slug": "original-track",
        "source_run_id": 101, "role": "baseline", "file": "my-baseline.yaml",
        "content_sha256": "-",
    }
    assert not any("original-track" in f["text"] for f in out["files"] if f["kind"] != "validation")


@pytest.mark.parametrize(
    "variants,code",
    [
        ([{"run_id": 103}], 409),  # never measured
        ([{"run_id": 999}], 404),
        ([{"role": "baseline"}], 422),  # no run
        ([], 422),
    ],
)
async def test_preview_refuses_what_it_cannot_publish(stack, variants, code):
    http, factory = stack
    await _seed(factory)
    res = await http.post(
        "/api/catalog-export/preview", json={"variants": variants, "use_catalog": False}
    )
    assert res.status_code == code, res.text


async def test_an_env_var_settled_once_applies_to_every_later_export(stack):
    http, factory = stack
    await _seed(factory)
    async with factory() as s:
        campaign = await s.get(Campaign, 9)
        campaign.extra_env = {"SOMETHING_NEW": "1"}
        await s.commit()
    body = {"variants": PAIR[:1], "hf": HF, "use_catalog": False}

    def env_rows(out):
        return {i["name"]: (i["category"], i["action"])
                for i in out["variants"][0]["inputs"] if i["kind"] == "env"}

    first = (await http.post("/api/catalog-export/preview", json=body)).json()
    assert env_rows(first)["SOMETHING_NEW"] == ("unknown", "kept")
    once = await http.post("/api/catalog-export/preview",
                           json={**body, "env_categories": {"SOMETHING_NEW": "site"}})
    assert env_rows(once.json())["SOMETHING_NEW"] == ("site", "left out"), "this export only"

    res = await http.put("/api/catalog-export/env-categories/SOMETHING_NEW",
                         json={"category": "site"})
    assert res.status_code == 200 and res.json()["set_by"] == "admin"
    later = (await http.post("/api/catalog-export/preview", json=body)).json()
    assert env_rows(later)["SOMETHING_NEW"] == ("site", "left out"), "settled for good"
    listed = (await http.get("/api/catalog-export/env-categories")).json()
    assert listed == [{"name": "SOMETHING_NEW", "category": "site", "set_by": "admin"}]
    assert (await http.put("/api/catalog-export/env-categories/bad-name",
                           json={"category": "site"})).status_code == 422
    assert (await http.put("/api/catalog-export/env-categories/X",
                           json={"category": "weird"})).status_code == 422
    gone = await http.delete("/api/catalog-export/env-categories/SOMETHING_NEW")
    assert gone.status_code == 204
    back = (await http.post("/api/catalog-export/preview", json=body)).json()
    assert env_rows(back)["SOMETHING_NEW"] == ("unknown", "kept")


async def test_a_saved_report_on_the_same_runs_is_used_before_writing_one(stack):
    from app.db.models import AgentReport

    http, factory = stack
    await _seed(factory)
    async with factory() as s:
        s.add(AgentReport(id=7, title="Hand-written", lang="en", baseline_run_id=101,
                          attempt_run_ids=[102], markdown="# Hand-written", comparison={}))
        s.add(AgentReport(id=8, title="Other runs", lang="en", baseline_run_id=101,
                          attempt_run_ids=[103], markdown="# Other", comparison={}))
        await s.commit()
    out = (await http.post("/api/catalog-export/preview",
                           json={"variants": PAIR, "hf": HF, "use_catalog": False})).json()
    assert out["report_source"] == "saved:7"
    report = out["files"][3]
    assert report["path"] == "models/qwen3.5-35b-a3b/qwen3.5-35b-a3b-h800-report.html"
    assert "Hand-written" in report["text"]
    meta = yaml.safe_load(out["files"][2]["text"])
    assert meta["tuning"][0]["report"] == "qwen3.5-35b-a3b-h800-report.html"
