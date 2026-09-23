"""GPU-type vocabulary, cluster capacity probe, and per-run card provenance.

The platform's comparison model leans on card type being a controlled value read
from the cluster, not a hand-typed string. These pin the three moving parts:
the normalizer (product string -> canonical), the driver's node probe (count +
type from the cluster), and the run's actual-card snapshot.
"""

import pytest

from app.control.launch.base import MachineInfo
from app.hardware import GPU_TYPES, coerce_gpu_type, normalize_gpu_type
from app.schemas.core import BaselineIn, MachineCreate
from tests.test_k8s_driver import FakeK8sApi, _clone, _driver, _spec


def _node(name: str, product: str | None, gpus: int) -> dict:
    labels = {"nvidia.com/gpu.product": product} if product is not None else {}
    return {
        "metadata": {"name": name, "labels": labels},
        # Real k8s reports allocatable as a string ("8"); the reader coerces.
        "status": {"allocatable": {"nvidia.com/gpu": str(gpus)}},
    }


# -- the vocabulary -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NVIDIA-H100-80GB-HBM3", "H100"),
        ("NVIDIA-A100-SXM4-80GB", "A100"),
        ("A100-SXM4-80GB", "A100"),          # legacy hand-typed value
        ("NVIDIA-H800", "H800"),
        ("nvidia-h100", "H100"),             # case-insensitive
        ("NVIDIA-B300", "B300"),
        ("NVIDIA H200 80GB", "H200"),        # space separator, not "H20"
        ("NVIDIA-H20", "H20"),               # and H20 is not read out of H200
        ("NVIDIA-L40S", ""),                 # a card not in the vocabulary yet
        ("", ""),
    ],
)
def test_normalize_maps_products_and_legacy_strings_to_canonical(raw, expected):
    assert normalize_gpu_type(raw) == expected


def test_h200_and_h20_do_not_bleed_into_each_other():
    # The boundary match is the whole point — a substring rule would call both H20.
    assert normalize_gpu_type("NVIDIA-H200-141GB") == "H200"
    assert normalize_gpu_type("NVIDIA-H20-96GB") == "H20"


def test_coerce_passes_empty_normalizes_legacy_and_rejects_unknown():
    assert coerce_gpu_type("") == ""
    assert coerce_gpu_type("   ") == ""
    assert coerce_gpu_type("A100-SXM4-80GB") == "A100"
    with pytest.raises(ValueError):
        coerce_gpu_type("definitely-not-a-gpu")


def test_schemas_canonicalize_and_reject():
    # A machine and a baseline share the vocabulary; a typo is a 422 at entry.
    assert MachineCreate(name="m", host="h", gpu_type="A100-SXM4-80GB").gpu_type == "A100"
    assert BaselineIn(served_model_name="x", card_type="NVIDIA-H100-80GB-HBM3").card_type == "H100"
    with pytest.raises(ValueError):
        MachineCreate(name="m", host="h", gpu_type="bogus")


# -- the capacity probe -------------------------------------------------------


def _probe(nodes: list[dict], selector: str) -> dict:
    settings = _clone()
    api = FakeK8sApi(settings)
    api.nodes = nodes
    driver = _driver(settings, api)
    return driver.probe_capacity(
        MachineInfo(name="pool", host="", gpu_count=0, driver="k8s", node_selector=selector)
    )


def test_probe_reads_count_and_type_of_a_single_pinned_node():
    result = _probe(
        [_node("gpu-h100-1", "NVIDIA-H100-80GB-HBM3", 8)],
        "kubernetes.io/hostname=gpu-h100-1",
    )
    assert result["supported"] is True
    assert result["gpu_count"] == 8
    assert result["gpu_type"] == "H100"
    assert result["node_count"] == 1
    assert result["warnings"] == []


def test_probe_sums_a_type_pool_and_reports_the_shared_type():
    result = _probe(
        [_node("gpu-h100-1", "NVIDIA-H100-80GB-HBM3", 8),
         _node("gpu-h100-2", "NVIDIA-H100-80GB-HBM3", 8)],
        "nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3",
    )
    assert result["gpu_count"] == 16
    assert result["gpu_type"] == "H100"
    assert result["node_count"] == 2


def test_probe_warns_and_withholds_a_type_when_a_pool_spans_two_cards():
    result = _probe(
        [_node("gpu-a100-1", "NVIDIA-A100-SXM4-80GB", 8),
         _node("gpu-h100-1", "NVIDIA-H100-80GB-HBM3", 8)],
        "some-shared-label=true",
    )
    # Summed capacity is still meaningful, but a single card type is not — so it
    # is withheld and the mismatch is surfaced loudly.
    assert result["gpu_count"] == 16
    assert result["gpu_type"] == ""
    assert any("multiple card types" in w for w in result["warnings"])


def test_probe_warns_on_an_unknown_card_and_on_no_selector():
    unknown = _probe([_node("gpu-x", "NVIDIA-L40S-48GB", 8)], "kubernetes.io/hostname=gpu-x")
    assert unknown["gpu_type"] == ""
    assert any("unknown GPU product" in w for w in unknown["warnings"])

    none = _probe([_node("gpu-h100-1", "NVIDIA-H100-80GB-HBM3", 8)], "")
    assert none["node_count"] == 0
    assert any("no node selector" in w for w in none["warnings"])


def test_bare_metal_driver_cannot_probe():
    from app.control.launch import get_driver

    result = get_driver("ssh_docker").probe_capacity(
        MachineInfo(name="node-24", host="10.0.0.1", gpu_count=8)
    )
    assert result == {"supported": False}


# -- per-run card provenance (Layer C) ----------------------------------------


def test_environment_records_the_card_the_run_actually_landed_on():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    api.nodes = [_node("gpu-h100-1", "NVIDIA-H100-80GB-HBM3", 8)]
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())
    # The pod landed on gpu-h100-1, which the node read says is an H100.
    api.pods = [{"spec": {"nodeName": "gpu-h100-1"}, "status": {}}]

    snapshot = driver.environment(handle)
    assert snapshot["k8s_node"] == "gpu-h100-1"
    assert snapshot["card_type"] == "H100"


def test_gpu_types_vocabulary_is_exposed_for_the_forms():
    # The dropdowns read this list; the four the cluster will grow into are in it.
    for card in ("A100", "H100", "H800", "B300"):
        assert card in GPU_TYPES
