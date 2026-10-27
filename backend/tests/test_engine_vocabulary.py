"""Reading an engine image's vocabulary: the probe that runs inside the image,
and what the platform keeps of what it printed.

The probe is run here the way an image runs it (`python -c`, the engine
package on the path) against two tiny packages shaped like sglang's env
registry and vllm's env table, so its parsing is held to the real layouts
without an engine installed.
"""

import subprocess
import sys
import textwrap

import httpx
import pytest

from app.control.engines import vocabulary_store as vs
from app.control.launch.base import LaunchDeferred

SGLANG_ENVIRON = '''
import os

class EnvField:
    def __init__(self, default, secret=False):
        self.default = default

class EnvBool(EnvField): pass
class EnvInt(EnvField): pass
class EnvStr(EnvField): pass

def _default_hip():
    return False

class Envs:
    # ===================================================================
    # Scheduler token budgeting
    # ===================================================================
    # Initial new-token ratio used for memory planning.
    SGLANG_INIT_NEW_TOKEN_RATIO = EnvInt(7)
    SGLANG_ENABLE_JIT_DEEPGEMM = EnvBool(True)
    SGLANG_DEEPGEMM_STANDARD_LAYOUT = EnvStr("auto")

    # ===================================================================
    # AMD, ROCm, and AITER
    # ===================================================================
    SGLANG_USE_AITER = EnvBool(_default_hip)
    SGLANG_HF_TOKEN = EnvStr("", secret=True)
'''

SGLANG_READS = '''
import os
from sglang.srt.environ import envs

def layout():
    mode = envs.SGLANG_DEEPGEMM_STANDARD_LAYOUT.get().lower()
    if mode not in ("auto", "masked", "compact"):
        raise ValueError(mode)
_FAST = os.getenv("SGLANG_SOME_FAST_PATH", "0")

def check():
    # Removal notice for the retired env var.
    if os.getenv("SGLANG_ENABLE_SPEC_V2") is not None:
        print("SGLANG_ENABLE_SPEC_V2 has been removed: v2 always runs")
'''

VLLM_ENVS = '''
import os

def env_with_choices(name, default, choices):
    return lambda: os.getenv(name, default)

environment_variables = {
    # ================== Runtime Env Vars ==================
    # Whether to use the FlashInfer sampler.
    "VLLM_USE_FLASHINFER_SAMPLER": lambda: bool(int(os.getenv("VLLM_USE_FLASHINFER_SAMPLER", "1"))),
    # The all2all backend for expert parallelism.
    "VLLM_ALL2ALL_BACKEND": env_with_choices("VLLM_ALL2ALL_BACKEND", "naive", ["naive", "deepep"]),
    "VLLM_ROCM_USE_AITER": lambda: os.getenv("VLLM_ROCM_USE_AITER", "0").lower() in ("1", "true"),
    "VLLM_PORT": lambda: int(os.getenv("VLLM_PORT", "0")),
}
'''


def _probe(tmp_path, engine: str) -> dict:
    root = tmp_path / engine
    if engine == "sglang":
        (root / "srt" / "layers").mkdir(parents=True)
        (root / "__init__.py").write_text("raise ImportError('never imported')\n")
        (root / "srt" / "__init__.py").write_text("")
        (root / "srt" / "environ.py").write_text(SGLANG_ENVIRON)
        (root / "srt" / "layers" / "fast.py").write_text(SGLANG_READS)
    else:
        root.mkdir()
        (root / "__init__.py").write_text("raise ImportError('never imported')\n")
        (root / "envs.py").write_text(VLLM_ENVS)
    code = vs._PROBE.read_text()
    out = subprocess.run(
        [sys.executable, "-c", code, engine], capture_output=True, text=True,
        env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"}, timeout=60, check=True,
    )
    return vs.parse_output(out.stdout)


def test_the_probe_reads_sglangs_registry_and_its_stray_reads(tmp_path):
    payload = _probe(tmp_path, "sglang")
    # The flags need the real engine; the env half still comes back.
    assert payload["status"] == "partial"
    assert any("flags unreadable" in n for n in payload["notes"])
    assert any("SGLANG_ENABLE_SPEC_V2: read only to say it was removed" in n
               for n in payload["notes"])
    env = {e["name"]: e for e in payload["entries"]}
    ratio = env["SGLANG_INIT_NEW_TOKEN_RATIO"]
    assert (ratio["type"], ratio["default"]) == ("int", 7)
    assert ratio["help"] == "Initial new-token ratio used for memory planning."
    assert ratio["section"] == "Scheduler token budgeting"
    assert env["SGLANG_USE_AITER"]["default"] == "(computed)"
    assert env["SGLANG_USE_AITER"]["section"] == "AMD, ROCm, and AITER"
    assert env["SGLANG_HF_TOKEN"]["secret"] is True
    stray = env["SGLANG_SOME_FAST_PATH"]
    assert stray["help_source"] == "source" and stray["where"] == "srt/layers/fast.py:9"
    assert "SGLANG_ENABLE_SPEC_V2" not in env
    # No declared choices for a string var: the engine's own check of it.
    [read] = env["SGLANG_DEEPGEMM_STANDARD_LAYOUT"]["reads"]
    assert read["where"] == "srt/layers/fast.py:6"
    assert 'not in ("auto", "masked", "compact")' in read["code"]


def test_the_probe_reads_vllms_env_table(tmp_path):
    env = {e["name"]: e for e in _probe(tmp_path, "vllm")["entries"]}
    sampler = env["VLLM_USE_FLASHINFER_SAMPLER"]
    assert (sampler["type"], sampler["default"]) == ("bool", True)
    assert sampler["help"] == "Whether to use the FlashInfer sampler."
    assert sampler["section"] == "Runtime Env Vars"
    backend = env["VLLM_ALL2ALL_BACKEND"]
    assert (backend["default"], backend["choices"]) == ("naive", ["naive", "deepep"])
    assert env["VLLM_ROCM_USE_AITER"]["type"] == "bool"


def test_ingest_keeps_what_a_config_may_set_and_says_what_it_is_for(tmp_path):
    payload = _probe(tmp_path, "vllm")
    payload["entries"] += [
        {"kind": "flag", "name": "tensor_parallel_size", "type": "int", "help": "TP size."},
        {"kind": "flag", "name": "port", "type": "int", "help": "Port."},
        {"kind": "flag", "name": "max_num_seqs", "flag": "--max-num-seqs", "help": "Seqs."},
        {"kind": "flag", "name": "max_num_seqs", "flag": "--max-seqs", "help": "Deprecated alias."},
    ]
    entries, notes = vs.ingest("vllm", payload, None)
    names = {(e["kind"], e["name"]): e for e in entries}
    assert ("flag", "tp") in names  # the platform's canonical key
    assert ("flag", "port") not in names  # placement
    # A deprecated spelling sharing the flag's name stays a note on it.
    assert names[("flag", "max_num_seqs")]["help"] == "Seqs."
    assert names[("flag", "max_num_seqs")]["deprecated_flags"] == ["--max-seqs"]
    assert ("env", "VLLM_PORT") not in names
    assert notes == ["1 env vars left out: credentials or placement"]
    assert names[("env", "VLLM_ROCM_USE_AITER")]["hardware"] == ["amd"]
    assert names[("env", "VLLM_USE_FLASHINFER_SAMPLER")]["hardware"] == []
    assert names[("env", "VLLM_USE_FLASHINFER_SAMPLER")]["help_source"] == "engine"


def test_sglang_docs_fill_in_what_the_code_does_not_say(tmp_path):
    docs = vs.parse_sglang_docs(textwrap.dedent("""
        ## DeepGEMM Configuration
        <table><tbody>
            <tr>
              <td style={{padding: "9px"}}>`SGLANG_ENABLE_JIT_DEEPGEMM`</td>
              <td style={{padding: "9px"}}>Enable JIT compilation of DeepGEMM kernels &amp; more
              </td>
              <td style={{padding: "9px"}}>`true`</td>
            </tr>
        </tbody></table>
        ## AMD &amp; ROCm
        <table><tbody><tr><td>`SGLANG_USE_AITER`</td><td>Use AITER</td><td>`false`</td></tr>
        </tbody></table>
    """))
    assert docs["SGLANG_ENABLE_JIT_DEEPGEMM"] == {
        "help": "Enable JIT compilation of DeepGEMM kernels & more", "default": "true",
        "section": "DeepGEMM Configuration",
    }
    entries, _ = vs.ingest("sglang", _probe(tmp_path, "sglang"), docs)
    env = {e["name"]: e for e in entries if e["kind"] == "env"}
    jit = env["SGLANG_ENABLE_JIT_DEEPGEMM"]
    assert (jit["help_source"], jit["help"]) == ("docs", docs["SGLANG_ENABLE_JIT_DEEPGEMM"]["help"])
    # The code's own comment wins over the docs.
    assert env["SGLANG_INIT_NEW_TOKEN_RATIO"]["help_source"] == "engine"
    assert env["SGLANG_USE_AITER"]["hardware"] == ["amd"]
    assert "SGLANG_HF_TOKEN" not in env


def test_the_docs_are_optional(monkeypatch):
    def unreachable(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", unreachable)
    docs, note = vs.fetch_docs("sglang", "0.5.19")
    assert docs is None and "not fetched" in note and "v0.5.19" in note
    assert vs.fetch_docs("vllm", "0.32.0") == (None, "")  # vllm's comments are in the image
    monkeypatch.setattr(vs.get_settings(), "engine_docs_url_sglang", "")
    assert vs.fetch_docs("sglang", "0.5.19") == (None, "")


class _Driver:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_in_image(self, machine, image, command, timeout):
        self.calls.append((image, command))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_reading_an_image(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "fetch_docs", lambda engine, version: (None, ""))
    payload = _probe(tmp_path, "vllm")
    import base64
    import json
    import zlib

    blob = base64.b64encode(zlib.compress(json.dumps(payload).encode())).decode()
    driver = _Driver((f"engine noise\n{vs.MARKER}{blob}\n", "vllm@sha256:abc"))
    fields = vs.read_image(driver, object(), "vllm", "vllm/vllm-openai:v1")
    assert fields["image_digest"] == "vllm@sha256:abc"
    assert fields["status"] == "partial" and fields["entries"]
    assert driver.calls[0][1].startswith('python3 -c "import base64,zlib;exec(')

    failed = vs.read_image(_Driver(RuntimeError("exit 1: no python")), object(), "vllm", "x")
    assert failed["status"] == "failed" and "no python" in failed["notes"][0]
    with pytest.raises(LaunchDeferred):
        vs.read_image(_Driver(LaunchDeferred("not pulled")), object(), "vllm", "x")


# -- the rules and the label hints ---------------------------------------------------


def test_never_and_forbidden_come_from_the_common_rules_file():
    from app.control.engines.env_keys import PLACEMENT_ENV
    from app.control.engines.rules import default_forbidden, never
    from app.control.search.space import DEFAULT_FORBIDDEN_PARAMS

    assert "env.CUDA_VISIBLE_DEVICES" in never() and "CUDA_VISIBLE_DEVICES" in PLACEMENT_ENV
    assert DEFAULT_FORBIDDEN_PARAMS == default_forbidden()
    assert {"kv_cache_dtype", "env.*THINK*"} <= set(DEFAULT_FORBIDDEN_PARAMS)


def test_the_shipped_label_files_name_real_switches_shapes():
    from app.control.engines import vocabulary as v

    v.load_labels.cache_clear()
    for engine in ("sglang", "vllm"):
        for label in v.load_labels(engine):
            assert label.level in v.LEVELS
