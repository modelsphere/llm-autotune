"""Is a registered cluster the one AutoTune itself runs in?

It decides how a run can be reached. A Service name (`endpoint: dns`) resolves
only inside its own cluster, so it works when AutoTune and LLMBench run in the
same cluster as the engine, and nowhere else: a run on another cluster is
healthy and still unreachable, and fails its health check after the whole model
load. Another cluster's runs are reached through a NodePort on a node address
instead.

Told apart by the cluster's certificate authority: the CA in the cluster's
kubeconfig against the one Kubernetes mounts into AutoTune's own pod. Same CA,
same cluster. No API call, no permission, no network.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import yaml

# Where Kubernetes mounts the CA of the cluster a pod runs in.
OWN_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)

ANOTHER_CLUSTER = (
    "it is not the cluster AutoTune runs in, so a run's Service name does not "
    "resolve from AutoTune or LLMBench: reach runs by NodePort, with a node "
    "address both can reach as the public host"
)


def _certs(pem: bytes) -> set[bytes]:
    """The certificates in a PEM bundle, as DER bytes (whitespace-proof)."""
    out = set()
    for body in _PEM.findall(pem):
        try:
            out.add(base64.b64decode(b"".join(body.split())))
        except ValueError:
            continue
    return out


def _kubeconfig_ca(doc: dict[str, Any], context: str = "") -> bytes | None:
    """The CA bundle of the kubeconfig's cluster for `context` (or its current
    one). None when it names none (insecure, or a CA file we cannot read)."""
    ctx_name = context or doc.get("current-context") or ""
    contexts = doc.get("contexts") or []
    ctx = next((c for c in contexts if c.get("name") == ctx_name), None)
    if ctx is None and len(contexts) == 1:
        ctx = contexts[0]
    cluster_name = ((ctx or {}).get("context") or {}).get("cluster")
    clusters = doc.get("clusters") or []
    entry = next((c for c in clusters if c.get("name") == cluster_name), None)
    if entry is None and len(clusters) == 1:
        entry = clusters[0]
    spec = (entry or {}).get("cluster") or {}
    if spec.get("certificate-authority-data"):
        try:
            return base64.b64decode(spec["certificate-authority-data"])
        except ValueError:
            return None
    if spec.get("certificate-authority"):
        try:
            return Path(spec["certificate-authority"]).read_bytes()
        except OSError:
            return None
    return None


def same_cluster(cluster: Any, own_ca: Path | None = None) -> bool | None:
    """True: the cluster AutoTune runs in. False: another one. None: cannot
    tell (AutoTune is not running in a pod, or the kubeconfig names no CA)."""
    if getattr(cluster, "k8s_in_cluster", False):
        return True
    try:
        own = _certs((own_ca or OWN_CA).read_bytes())
    except OSError:
        return None
    text = getattr(cluster, "kubeconfig_content", "") or ""
    if not text and getattr(cluster, "k8s_kubeconfig", ""):
        try:
            text = Path(cluster.k8s_kubeconfig).read_text()
        except OSError:
            return None
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    ca = _kubeconfig_ca(doc, getattr(cluster, "k8s_context", "") or "")
    theirs = _certs(ca) if ca else set()
    if not own or not theirs:
        return None
    # A bundle during CA rotation holds two: sharing one is the same cluster.
    return bool(own & theirs)


def unreachable_by_name(cluster: Any, own_ca: Path | None = None) -> bool:
    """A cluster whose runs are named by Service DNS while it is known to be
    another cluster: every run on it would load and then be unreachable."""
    return (
        getattr(cluster, "k8s_workload_kind", "") == "llmservice"
        and (getattr(cluster, "k8s_endpoint", "") or "dns") == "dns"
        and same_cluster(cluster, own_ca) is False
    )
