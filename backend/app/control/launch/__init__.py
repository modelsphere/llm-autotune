from app.control.launch.base import (
    DeploymentDriver,
    DeploymentHandle,
    DeploymentState,
    LaunchSpec,
    MachineInfo,
    NodeAssignment,
    merge_volumes,
)
from app.control.launch.k8s import K8sDriver
from app.control.launch.ssh_docker import SshDockerDriver

DRIVER_REGISTRY: dict[str, type[DeploymentDriver]] = {
    "ssh_docker": SshDockerDriver,
    "k8s": K8sDriver,
}


def get_driver(name: str = "ssh_docker", cluster_id: int | None = None) -> DeploymentDriver:
    # Empty name means "the platform default" — the same convention MachineInfo
    # and the Machine column use, so a machine that names no substrate resolves
    # here rather than needing every caller to special-case it.
    #
    # `cluster_id` is the k8s machine's cluster (None = the platform default).
    # The ssh driver ignores it — a bare-metal box is not in a cluster.
    try:
        factory = DRIVER_REGISTRY[name or "ssh_docker"]
    except KeyError as exc:
        raise ValueError(f"unknown driver '{name}' (available: {list(DRIVER_REGISTRY)})") from exc
    if factory is K8sDriver:
        return K8sDriver(cluster_id=cluster_id)
    return factory()


__all__ = [
    "DeploymentDriver",
    "DeploymentHandle",
    "DeploymentState",
    "K8sDriver",
    "LaunchSpec",
    "MachineInfo",
    "NodeAssignment",
    "SshDockerDriver",
    "get_driver",
    "merge_volumes",
]
