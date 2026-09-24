"""Cheap checks for expensive failures.

Each of these corresponds to a way a night has actually been lost: an image
that had to pull inside the run's clock, a bind mount docker silently created
as an empty directory, a port kube-proxy was hijacking. The parsing is boring;
the classification is not — a check that says FAIL when it means "this will be
slower" trains people to ignore the ones that matter.
"""

from app.control.launch import preflight as pf


class _Result:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _Driver:
    """Returns canned probe output instead of running ssh."""

    def __init__(self, stdout: str, returncode: int = 0, raises: Exception | None = None):
        self._result = _Result(stdout, returncode)
        self._raises = raises
        self.scripts: list[str] = []

    def _ssh(self, machine, script, timeout=45):
        if self._raises:
            raise self._raises
        self.scripts.append(script)
        return self._result


class _Machine:
    name = "node-24"
    host = "10.0.0.1"
    ssh_user = "root"
    ssh_port = 22
    gpu_count = 8


HEALTHY = """
ssh ok
image local
model ok
ports 22 80 8050
portholder
kube no
gpus 8
"""


def _with_ports(*busy: int, holder: str = "") -> str:
    listed = " ".join(str(p) for p in busy)
    return HEALTHY.replace("ports 22 80 8050", f"ports 22 80 {listed}").replace(
        "portholder", f"portholder {holder}"
    )


def _run(stdout, **kw):
    return pf.inspect(
        _Driver(stdout), _Machine(),
        image=kw.pop("image", "sglang:v1"),
        model_path=kw.pop("model_path", "/data/model"),
        volumes=kw.pop("volumes", {}),
        port=kw.pop("port", 28200),
        **kw,
    )


def _by_key(checks, key):
    return next(c for c in checks if c.key == key)


# -- the happy path -----------------------------------------------------------


def test_a_healthy_machine_passes_everything():
    checks = _run(HEALTHY)
    assert {c.status for c in checks} == {pf.PASS}


# -- things that stop a run ---------------------------------------------------


def test_an_unreachable_machine_is_a_single_blocking_finding():
    """And not five separate ones: if ssh is down, everything else is unknown
    rather than broken, and listing six failures hides the one that matters."""
    checks = pf.inspect(
        _Driver("", raises=RuntimeError("ssh timed out")), _Machine(),
        image="i", model_path="/m", volumes={}, port=28200,
    )
    assert len(checks) == 1
    assert checks[0].key == "ssh" and checks[0].status == pf.FAIL


def test_a_missing_model_path_blocks():
    checks = _run(HEALTHY.replace("model ok", "model missing"))
    assert _by_key(checks, "model_path").status == pf.FAIL


def test_a_missing_bind_mount_blocks():
    """Docker would create it as an empty directory, so the container starts
    and then behaves as if the file were simply absent — the most confusing
    possible symptom."""
    checks = _run(
        HEALTHY.replace("model ok", "model ok\nvolume missing /root/moe-config"),
        volumes={"/root/moe-config": "/moe-config"},
    )
    bad = _by_key(checks, "volume:/root/moe-config")
    assert bad.status == pf.FAIL
    assert "empty directory" in bad.hint


def test_a_busy_base_port_is_normal_when_a_machine_is_shared():
    """Observed live: an operator ran this while another campaign held node-24 and
    was told the machine was blocked. It was not — the scheduler starts at the
    campaign's port and walks forward, which is how two runs share a box."""
    checks = _run(_with_ports(28200, holder='users:(("python3",pid=2945025))'))
    port = _by_key(checks, "port")
    assert port.status == pf.WARN
    assert "28201" in port.detail, "say which port the next run actually gets"


def test_a_fully_taken_port_window_blocks():
    checks = _run(_with_ports(*range(28200, 28208)))
    port = _by_key(checks, "port")
    assert port.status == pf.FAIL
    assert "28207" in port.detail


def test_a_gap_inside_the_window_is_still_usable():
    checks = _run(_with_ports(28200, 28201, 28202))
    port = _by_key(checks, "port")
    assert port.status == pf.WARN
    assert "28203" in port.detail


def test_a_nodeport_range_port_on_a_kube_host_blocks():
    """The failure this exists to prevent looks like a 30-minute readiness
    timeout: the engine binds fine and is reachable only from localhost."""
    checks = _run(HEALTHY.replace("kube no", "kube yes"), port=30001)
    assert _by_key(checks, "port_range").status == pf.FAIL


def test_the_same_port_is_fine_on_a_host_without_kube_proxy():
    checks = _run(HEALTHY, port=30001)
    assert not [c for c in checks if c.key == "port_range"]


def test_a_candidate_too_wide_for_the_machine_blocks():
    checks = _run(HEALTHY, widest_candidate_cards=16)
    gpus = _by_key(checks, "gpus")
    assert gpus.status == pf.FAIL
    assert "16" in gpus.detail and "8" in gpus.detail


def test_a_candidate_that_fits_passes():
    assert _by_key(_run(HEALTHY, widest_candidate_cards=8), "gpus").status == pf.PASS


def test_docker_missing_blocks():
    checks = _run(HEALTHY.replace("image local", "docker missing"))
    assert _by_key(checks, "image").status == pf.FAIL


# -- things that only cost you ------------------------------------------------


def test_an_absent_image_warns_rather_than_blocks():
    """The run will pull it. That is legitimate — but the pull happens inside
    the run's own clock, so it is worth knowing before rather than after."""
    checks = _run(HEALTHY.replace("image local", "image absent"))
    image = _by_key(checks, "image")
    assert image.status == pf.WARN
    assert "pull" in image.detail


def test_no_nvidia_smi_warns_rather_than_blocks():
    """Correct for the CPU-only platform host, which is a real machine in this
    fleet — blocking would make it impossible to test anything there."""
    assert _by_key(_run(HEALTHY.replace("gpus 8", "gpus none")), "gpus").status == pf.WARN


# -- policy images -----------------------------------------------------------


def test_a_present_policy_image_passes():
    checks = _run(HEALTHY + "polimg ok autotune-policy-rand:latest\n",
                  policy_images=["autotune-policy-rand:latest"])
    assert _by_key(checks, "policy_image:autotune-policy-rand:latest").status == pf.PASS


def test_an_absent_local_only_policy_image_blocks():
    """A bare name:tag has no registry to pull from here (policy images are
    docker-loaded onto the box), so an absent one wedges the policy at launch —
    a hard failure, unlike the engine image from a registry."""
    checks = _run(HEALTHY + "polimg absent autotune-policy-rand:latest\n",
                  policy_images=["autotune-policy-rand:latest"])
    assert _by_key(checks, "policy_image:autotune-policy-rand:latest").status == pf.FAIL


def test_an_absent_but_pullable_policy_image_only_warns():
    checks = _run(HEALTHY + "polimg absent registry.example.com/team/policy:latest\n",
                  policy_images=["registry.example.com/team/policy:latest"])
    assert _by_key(checks, "policy_image:registry.example.com/team/policy:latest").status == pf.WARN


def test_the_probe_asks_about_every_policy_image():
    driver = _Driver(HEALTHY)
    pf.inspect(driver, _Machine(), image="i", model_path="/m", volumes={}, port=28200,
               policy_images=["one:latest", "two:latest"])
    assert "docker image inspect one:latest" in driver.scripts[0]
    assert "docker image inspect two:latest" in driver.scripts[0]


# -- the parity finding -------------------------------------------------------


def test_matching_production_passes():
    assert pf.parity_check([], "prod-container").status == pf.PASS


def test_differing_from_production_warns_and_never_blocks():
    """Running with engine defaults is a legitimate thing to measure, and
    sometimes the whole point. It should be a decision, not a discovery at 3am."""
    missing = [{"flag": "enable_metrics", "production": True, "container": "prod"}]
    check = pf.parity_check(missing, "prod")
    assert check.status == pf.WARN
    assert "enable_metrics" in check.detail


def test_a_long_parity_list_is_truncated_rather_than_dumped():
    """Twelve flags on one line is the wall of text this replaced."""
    missing = [
        {"flag": f"flag_{i}", "production": True, "container": "prod"} for i in range(12)
    ]
    check = pf.parity_check(missing, "prod")
    assert "+4 more" in check.detail


def test_a_valued_flag_shows_its_value_and_a_boolean_does_not():
    missing = [
        {"flag": "chunked_prefill_size", "production": 16384, "container": "p"},
        {"flag": "enable_metrics", "production": True, "container": "p"},
    ]
    detail = pf.parity_check(missing, "p").detail
    assert "chunked_prefill_size=16384" in detail
    assert "enable_metrics," in detail or detail.endswith("enable_metrics")


# -- the benchmark ------------------------------------------------------------


CATALOG = {
    "autotune-test-v0": ["functional_acceptance", "perf_guidellm_sweep"],
    "rolling-replay-test-mf-v0": ["replay"],
}


def _bench(slug, target="", catalog=CATALOG):
    return pf.benchmark_check("benchmark", "Benchmark", slug, catalog, target)


def test_a_benchmark_that_can_report_the_objective_passes():
    check = _bench("rolling-replay-test-mf-v0", "replay.score_card_norm")
    assert check.status == pf.PASS
    assert "replay" in check.detail


def test_a_slug_that_does_not_exist_blocks():
    check = _bench("rolling-replay-test-mf-v1", "replay.score")
    assert check.status == pf.FAIL
    assert "rolling-replay-test-mf-v0" in check.hint, "suggest the near miss"


def test_an_objective_the_benchmark_cannot_report_blocks():
    """The expensive half of a staged campaign, pointed at the screening
    objective: every replay run completes, returns a full metric set, scores
    None, and the morning report says nothing held its redlines."""
    check = _bench("rolling-replay-test-mf-v0", "perf_guidellm_sweep.output_tpm_card_norm")
    assert check.status == pf.FAIL
    assert "replay" in check.detail and "perf_guidellm_sweep" in check.detail


def test_a_second_run_of_the_same_module_is_addressable_by_its_instance_key():
    """sweep-test runs perf_guidellm_sweep twice; the harvest files the second
    run under `perf_guidellm_sweep#2`, and the catalog lists it that way."""
    catalog = {"sweep-test": ["perf_guidellm_sweep", "perf_guidellm_sweep#2"]}
    ok = _bench("sweep-test", "perf_guidellm_sweep#2.output_tpm_card_norm", catalog)
    assert ok.status == pf.PASS
    assert _bench("sweep-test", "perf_guidellm_sweep#3.output_tps", catalog).status == pf.FAIL


def test_a_metric_with_no_module_is_not_second_guessed():
    """`score_total` is the submission's, not any one module's."""
    assert _bench("rolling-replay-test-mf-v0", "score_total").status == pf.PASS


def test_an_empty_slug_is_the_platform_default_not_a_missing_benchmark():
    assert _bench("", "").status == pf.PASS


def test_an_unreachable_benchmark_platform_is_skipped_not_failed():
    """Whether LLMBench answers right now is a fact about LLMBench. Blocking a
    campaign on it would make the whole form unusable during a deploy."""
    check = _bench("anything", "replay.score", catalog=None)
    assert check.status == pf.SKIP


# -- the search space ---------------------------------------------------------


def test_a_space_with_no_candidates_blocks():
    assert pf.space_check(0, []).status == pf.FAIL


def test_a_malformed_space_blocks_with_its_own_reason():
    check = pf.space_check(4, ["tied group columns differ in length"])
    assert check.status == pf.FAIL
    assert "tied group" in check.detail


# -- the verdict --------------------------------------------------------------


def test_warnings_alone_do_not_make_a_campaign_unrunnable():
    report = pf.summarize("node-24", _run(HEALTHY.replace("image local", "image absent")))
    assert report["ok"] is True
    assert report["warnings"] == 1


def test_one_failure_makes_it_unrunnable():
    report = pf.summarize("node-24", _run(HEALTHY.replace("model ok", "model missing")))
    assert report["ok"] is False
    assert report["failed"] == 1


# -- the probe script ---------------------------------------------------------


def test_paths_are_quoted_so_a_space_cannot_become_two_arguments():
    driver = _Driver(HEALTHY)
    pf.inspect(driver, _Machine(), image="img:1", model_path="/data/my model",
               volumes={"/a b": "/c"}, port=28200)
    script = driver.scripts[0]
    assert "'/data/my model'" in script
    assert "'/a b'" in script


# -- multi-node: the interconnect a gang adds ---------------------------------


class _Node:
    """A member as `inspect_group` sees it: a MachineInfo-shaped record."""

    def __init__(self, name: str, host: str, *, data_host: str = "",
                 gpu_count: int = 8, gpu_type: str = "A100",
                 nccl_ifname: str = "", driver: str = "ssh_docker"):
        self.name = name
        self.host = host
        self.data_host = data_host
        self.gpu_count = gpu_count
        self.gpu_type = gpu_type
        self.nccl_ifname = nccl_ifname
        self.driver = driver

    @property
    def interior_host(self) -> str:
        return self.data_host or self.host


def _group(ranked, **over):
    return pf.inspect_group(
        _Driver(over.pop("stdout", HEALTHY)), ranked,
        image="img:1", model_path="/m", volumes={}, service_port=28200, **over,
    )


def test_a_worker_is_told_to_reach_the_masters_interior_address():
    nodes = [
        (0, _Node("node-1", "10.0.0.1", data_host="192.168.9.1")),
        (1, _Node("node-2", "10.0.0.2")),
    ]
    driver = _Driver(HEALTHY + "\npeer ok 192.168.9.1\nnic ok ib0\nib 2\n")
    pf.inspect_group(
        driver, nodes, image="i", model_path="/m", volumes={}, service_port=28200,
        nccl_env={"NCCL_SOCKET_IFNAME": "ib0"},
    )
    # The master is not asked to reach itself; the worker is.
    assert "ping -c1 -W2 192.168.9.1" not in driver.scripts[0]
    assert "ping -c1 -W2 192.168.9.1" in driver.scripts[1]


def test_an_unreachable_master_is_a_warning_not_a_blocker():
    """ICMP can be filtered, and this probe cannot tell that from a bad route —
    so it must not block a group the launch itself would confirm."""
    ranked = [
        (0, _Node("node-1", "10.0.0.1", data_host="192.168.9.1")),
        (1, _Node("node-2", "10.0.0.2")),
    ]
    by_name = {row["machine"]: row for row in _group(
        ranked, stdout=HEALTHY + "\npeer unreachable 192.168.9.1\nnic ok ib0\nib 2\n",
        nccl_env={"NCCL_SOCKET_IFNAME": "ib0"},
    )}
    interior = next(c for c in by_name["node-2"]["checks"] if c["key"] == "interior")
    assert interior["status"] == pf.WARN
    assert by_name["node-2"]["ok"] is True


def test_a_missing_nccl_interface_blocks():
    ranked = [
        (0, _Node("node-1", "10.0.0.1")),
        (1, _Node("node-2", "10.0.0.2")),
    ]
    by_name = {row["machine"]: row for row in _group(
        ranked, stdout=HEALTHY + "\nnic missing ib9\nib 2\n",
        nccl_env={"NCCL_SOCKET_IFNAME": "ib9"},
    )}
    nic = next(c for c in by_name["node-1"]["checks"] if c["key"] == "nic")
    assert nic["status"] == pf.FAIL
    assert by_name["node-1"]["ok"] is False


def test_asking_for_rdma_without_a_device_warns():
    ranked = [(0, _Node("node-1", "10.0.0.1"))]
    row = _group(
        ranked, stdout=HEALTHY + "\nib 0\n", nccl_env={"NCCL_IB_HCA": "mlx5_0"},
    )[0]
    ib = next(c for c in row["checks"] if c["key"] == "ib")
    assert ib["status"] == pf.WARN


def test_mixed_members_fail_a_group_that_was_formed_before_they_changed():
    ranked = [
        (0, _Node("node-1", "10.0.0.1", gpu_type="A100")),
        (1, _Node("node-2", "10.0.0.2", gpu_type="H100")),
    ]
    by_name = {row["machine"]: row for row in _group(ranked)}
    parity = next(c for c in by_name["node-1"]["checks"] if c["key"] == "homogeneity")
    assert parity["status"] == pf.FAIL
    assert "mixed members" in parity["detail"]


def test_a_uniform_group_passes_parity():
    ranked = [
        (0, _Node("node-1", "10.0.0.1")),
        (1, _Node("node-2", "10.0.0.2")),
    ]
    by_name = {row["machine"]: row for row in _group(ranked)}
    parity = next(c for c in by_name["node-2"]["checks"] if c["key"] == "homogeneity")
    assert parity["status"] == pf.PASS
