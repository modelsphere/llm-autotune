"""Per-machine driver resolution.

The fleet is heterogeneous through the k8s migration, so a machine names its own
substrate and the supervisor resolves it per machine — while still honouring the
driver a test injects by hand, which is what keeps every existing tick-based
test working.
"""

from app.control.launch import K8sDriver, SshDockerDriver, get_driver
from app.control.launch.base import MachineInfo
from app.control.orchestrator.supervisor import Supervisor
from tests.fakes import NullDriver


def _supervisor_with(driver):
    sup = Supervisor(session_factory=lambda: None)
    sup.driver = driver  # the injection every tick-based test uses
    return sup


def test_empty_name_resolves_to_the_default_driver():
    assert isinstance(get_driver(""), SshDockerDriver)
    assert isinstance(get_driver(), SshDockerDriver)


def test_registry_knows_both_substrates():
    assert isinstance(get_driver("ssh_docker"), SshDockerDriver)
    assert isinstance(get_driver("k8s"), K8sDriver)


def test_a_machine_that_names_nothing_uses_the_injected_default():
    fake = NullDriver()
    sup = _supervisor_with(fake)
    # "" (the column default) and the injected driver's own name both resolve
    # to the injected fake — so a test that sets self.driver is never bypassed.
    assert sup._driver_for(MachineInfo(name="m", host="h", driver="")) is fake
    assert sup._driver_for(MachineInfo(name="m", host="h", driver="null")) is fake


def test_a_machine_that_names_k8s_gets_the_k8s_driver_cached():
    sup = _supervisor_with(NullDriver())
    info = MachineInfo(name="pool", host="svc", driver="k8s")
    first = sup._driver_for(info)
    assert isinstance(first, K8sDriver)
    # Built once and reused: resolving again hands back the same instance.
    assert sup._driver_for(info) is first


def test_mixed_fleet_routes_each_machine_to_its_own_substrate():
    sup = _supervisor_with(NullDriver())
    ssh_machine = MachineInfo(name="node-24", host="10.0.0.1", driver="ssh_docker")
    k8s_machine = MachineInfo(name="pool-a", host="svc", driver="k8s")
    assert isinstance(sup._driver_for(ssh_machine), SshDockerDriver)
    assert isinstance(sup._driver_for(k8s_machine), K8sDriver)
