"""Cheap checks for expensive failures.

Every problem here costs seconds to detect and a night to discover the other
way. A missing model path fails after the container starts, the image pulls and
the engine reaches the point of opening weights — twenty minutes in, on a
machine whose production service has already been torn down to make room.

The rule for what belongs here: it must be answerable without launching
anything, and getting it wrong must cost real time. Anything requiring a real
deployment to know (does this config OOM at this batch size) is the search's
job, not preflight's — a check that cannot be trusted is worse than no check,
because people stop reading the ones next to it.
"""

import json
import shlex
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

PASS = "pass"
WARN = "warn"  # will probably work, but costs time or hides a surprise
FAIL = "fail"  # will not work; starting the campaign wastes the window
SKIP = "skip"  # could not be checked, and the reason is not the user's fault


@dataclass
class Check:
    key: str
    label: str
    status: str
    detail: str
    # Where to go to fix it, when the answer is not in the detail line.
    hint: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass
class Preflight:
    machine: str
    checks: list[Check] = field(default_factory=list)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def as_dict(self) -> dict:
        return {
            "machine": self.machine,
            "ok": not self.blocking,
            "failed": len(self.blocking),
            "warnings": len([c for c in self.checks if c.status == WARN]),
            "checks": [c.as_dict() for c in self.checks],
        }


# One remote script rather than eight round trips: ssh setup dominates the cost
# of any single check, and the whole point is that this feels instant enough to
# run on the way into a campaign.
_PROBE = r"""
set -u
echo "ssh ok"
{image_probe}
if [ -n {model_path} ]; then
  if [ -e {model_path} ]; then echo "model ok"; else echo "model missing"; fi
fi
{policy_probes}
{volume_probes}
echo "ports $(ss -lntp 2>/dev/null | awk 'NR>1 {{n=split($4,a,":"); print a[n]}}' \
  | sort -un | tr '\n' ' ')"
echo "portholder $(ss -lntp 2>/dev/null | awk '$4 ~ /:{port}$/ {{print $NF}}' | head -1)"
if pgrep -x kube-proxy >/dev/null 2>&1; then echo "kube yes"; else echo "kube no"; fi
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "gpus $(nvidia-smi -L 2>/dev/null | wc -l)"
else
  echo "gpus none"
fi
{interconnect_probes}
"""

# Multi-node additions. A gang's failure mode that costs the most is the one
# nothing else can see: the boxes are each fine, and they cannot talk to each
# other — or the engine is told to use an interface that does not exist. Both are
# seconds to check and a wrecked night to discover at rendezvous time.
_INTERCONNECT = r"""
if [ -n {peer_host} ]; then
  if ping -c1 -W2 {peer_host} >/dev/null 2>&1; then echo "peer ok {peer_host}"
  else echo "peer unreachable {peer_host}"; fi
fi
if [ -n {ifname} ]; then
  if ip link show {ifname} >/dev/null 2>&1; then echo "nic ok {ifname}"
  else echo "nic missing {ifname}"; fi
fi
echo "ib $(ls -1 /sys/class/infiniband 2>/dev/null | wc -l)"
"""


def _script(
    image: str, model_path: str, volumes: dict[str, str], port: int,
    policy_images: list[str] | tuple[str, ...] = (),
    peer_host: str = "", ifname: str = "",
) -> str:
    probes = "\n".join(
        f'if [ -e {shlex.quote(host)} ]; then echo "volume ok {host}"; '
        f'else echo "volume missing {host}"; fi'
        for host in sorted(volumes or {})
    )
    # Same one-shot rule as the engine image: a policy image absent at launch is
    # a wedge, not a search finding, so it is worth an ssh line here.
    policy_probes = "\n".join(
        f'if docker image inspect {shlex.quote(ref)} >/dev/null 2>&1; '
        f'then echo "polimg ok {ref}"; else echo "polimg absent {ref}"; fi'
        for ref in dict.fromkeys(p for p in (policy_images or ()) if p)
    )
    interconnect = ""
    if peer_host or ifname:
        interconnect = _INTERCONNECT.format(
            peer_host=shlex.quote(peer_host),
            ifname=shlex.quote(ifname),
        )
    # No image given (a group's fabric-only preflight) must not read as "the
    # image is missing on every member" — the check simply is not asked.
    image_probe = (
        f"if docker image inspect {shlex.quote(image)} >/dev/null 2>&1; "
        'then echo "image local"; else echo "image absent"; fi'
        if image
        else "command -v docker >/dev/null 2>&1 || echo 'docker missing'"
    )
    return _PROBE.format(
        image_probe=image_probe,
        model_path=shlex.quote(model_path or ""),
        policy_probes=policy_probes,
        volume_probes=probes,
        port=int(port),
        interconnect_probes=interconnect,
    )


def _lines(output: str) -> list[list[str]]:
    return [line.split() for line in output.splitlines() if line.strip()]


# How far the scheduler walks forward from a campaign's configured port looking
# for a free one. Mirrors `_free_port`: runs sharing a machine cannot share a
# port, because they share the host network namespace.
PORT_WINDOW = 8


def _holder(seen: list[list[str]]) -> str:
    for parts in seen:
        if parts[:1] == ["portholder"] and len(parts) > 1:
            return " ".join(parts[1:])
    return ""


def _port_check(port: int, listening: set[int], holder: str) -> Check:
    """Can a run get a port here?

    Not "is the configured port free": the scheduler starts at that port and
    walks forward, precisely so several runs can share a machine. Reporting a
    busy base port as a blocker called a normal, working arrangement broken —
    which was exactly what an operator saw the first time they ran this while
    another campaign was live on the same box.
    """
    free = [p for p in range(port, port + PORT_WINDOW) if p not in listening]
    if port not in listening:
        return Check("port", "Engine port", PASS, f"{port} is free")
    if free:
        return Check(
            "port", "Engine port", WARN,
            f"{port} is taken{f' by {holder}' if holder else ''}; "
            f"the next run gets {free[0]}",
            f"Normal when another campaign shares this machine — the scheduler walks "
            f"forward from {port} until it finds a free port.",
        )
    return Check(
        "port", "Engine port", FAIL,
        f"{port}–{port + PORT_WINDOW - 1} are all in use on this machine",
        "Nothing can be placed here until something frees a port, or the campaign is "
        "given a different starting port.",
    )


def _looks_pullable(ref: str) -> bool:
    """A registry-qualified image reference — one a machine can `docker pull` —
    names a registry host in its first path segment: a dot, a port colon, or
    `localhost`, and there is a `/` after it. A bare `name:tag` (no slash) has
    no registry here — the `:` is only the tag — so an absent one is a hard
    failure, not a pull-on-first-use warning."""
    if "/" not in ref:
        return False
    head = ref.split("/", 1)[0]
    return "." in head or ":" in head or head == "localhost"


def inspect(
    driver: Any,
    machine: Any,
    *,
    image: str,
    model_path: str,
    volumes: dict[str, str],
    port: int,
    widest_candidate_cards: int = 0,
    policy_images: list[str] | tuple[str, ...] = (),
    peer_host: str = "",
    nccl_ifname: str = "",
    expect_ib: bool = False,
    timeout: int = 45,
) -> list[Check]:
    """Run the machine-side probes. Never raises: an unreachable machine is a
    finding, not an exception — the caller is a form, not a pipeline.

    `peer_host` is the master's interior address when this machine is a WORKER:
    it turns "can this box reach the box it must rendezvous with" into a check.
    `nccl_ifname`/`expect_ib` are the group's fabric expectations, verified per
    member because a typo'd interface name is a launch-time NCCL failure with
    nothing in the engine log to explain it.
    """
    checks: list[Check] = []
    try:
        result = driver._ssh(
            machine,
            _script(image, model_path, volumes, port, policy_images, peer_host, nccl_ifname),
            timeout=timeout,
        )
    except Exception as exc:  # ssh timeout, host key, DNS…
        return [
            Check("ssh", "Machine reachable", FAIL, f"{machine.host}: {exc}",
                  "Check the host address, the ssh key, and that the box is up.")
        ]
    if result.returncode != 0 and "ssh ok" not in result.stdout:
        return [
            Check("ssh", "Machine reachable", FAIL,
                  (result.stderr or "ssh failed").strip()[:300],
                  "The platform launches containers over ssh; nothing runs until this works.")
        ]

    seen = _lines(result.stdout)
    checks.append(Check("ssh", "Machine reachable", PASS, f"ssh to {machine.host} works"))

    for parts in seen:
        head = parts[0]
        rest = parts[1:]

        if head == "image":
            if rest == ["local"]:
                checks.append(Check("image", "Container image", PASS,
                                    f"{image} is already on the machine"))
            else:
                # Not a failure: the first run pulls it. But the pull happens
                # inside the run's clock, so a large image or a registry that
                # needs credentials turns into a launch timeout on a machine
                # whose production service is already down.
                checks.append(Check(
                    "image", "Container image", WARN,
                    f"{image} is not on the machine; the first run will pull it",
                    "Pre-pull it to keep the pull out of the run's timeout, and to find "
                    "out now if the registry needs credentials.",
                ))
        elif head == "docker":
            checks.append(Check("image", "Docker", FAIL, "docker is not installed on the machine"))
        elif head == "model":
            if rest == ["ok"]:
                checks.append(Check("model_path", "Model path", PASS,
                                    f"{model_path} exists"))
            else:
                checks.append(Check(
                    "model_path", "Model path", FAIL,
                    f"{model_path} does not exist on {machine.name}",
                    "It is bind-mounted to /model; the engine fails after the image pull "
                    "and the model load begins.",
                ))
        elif head == "polimg":
            ok = rest[:1] == ["ok"]
            ref = rest[1] if len(rest) > 1 else "?"
            if ok:
                checks.append(Check(f"policy_image:{ref}", "Policy image", PASS,
                                    f"{ref} is on the machine"))
            elif _looks_pullable(ref):
                checks.append(Check(
                    f"policy_image:{ref}", "Policy image", WARN,
                    f"{ref} is not on the machine; the first entrant will pull it",
                    "Pre-pull it to keep the pull out of the entrant's launch clock.",
                ))
            else:
                checks.append(Check(
                    f"policy_image:{ref}", "Policy image", FAIL,
                    f"{ref} is not on {machine.name} and has no registry to pull from",
                    "Policy images aren't served from a registry here — load it onto the "
                    "machine (docker save … | ssh … docker load) before the entrant runs.",
                ))
        elif head == "volume":
            host_path = rest[1] if len(rest) > 1 else "?"
            if rest[:1] == ["ok"]:
                checks.append(Check(f"volume:{host_path}", "Bind mount", PASS,
                                    f"{host_path} exists"))
            else:
                checks.append(Check(
                    f"volume:{host_path}", "Bind mount", FAIL,
                    f"{host_path} does not exist on {machine.name}",
                    "Docker would create it as an empty directory, so the container starts "
                    "and then behaves as if the file were missing.",
                ))
        elif head == "ports":
            listening = {int(p) for p in rest if p.isdigit()}
            checks.append(_port_check(port, listening, _holder(seen)))
        elif head == "gpus":
            if rest == ["none"]:
                checks.append(Check("gpus", "GPUs", WARN, "nvidia-smi not found on the machine",
                                    "Fine for a CPU-only test host; not for a real run."))
            else:
                count = int(rest[0]) if rest and rest[0].isdigit() else 0
                if widest_candidate_cards and count and widest_candidate_cards > count:
                    checks.append(Check(
                        "gpus", "GPUs", FAIL,
                        f"the widest candidate needs {widest_candidate_cards} cards, "
                        f"{machine.name} has {count}",
                        "Narrow the search space or pick a bigger machine.",
                    ))
                else:
                    checks.append(Check("gpus", "GPUs", PASS, f"{count} visible"))
        elif head == "peer":
            target = rest[1] if len(rest) > 1 else peer_host
            if rest[:1] == ["ok"]:
                checks.append(Check(
                    "interior", "Interior link", PASS,
                    f"the master at {target} answers from {machine.name}",
                ))
            else:
                # A WARN rather than a FAIL on purpose: ICMP is filtered on some
                # fabrics, and this probe cannot tell "no route" from "no ping".
                # Blocking a valid group on that would be the untrustworthy
                # check the module's rule warns about; the launch itself confirms.
                checks.append(Check(
                    "interior", "Interior link", WARN,
                    f"{machine.name} could not ping the master at {target}",
                    "If the fabric filters ICMP this is noise. Otherwise check that "
                    "data_host names an address the other members can route to — a "
                    "gang that cannot reach its master hangs at rendezvous.",
                ))
        elif head == "nic":
            name = rest[1] if len(rest) > 1 else nccl_ifname
            if rest[:1] == ["ok"]:
                checks.append(Check("nic", "NCCL interface", PASS, f"{name} exists"))
            else:
                checks.append(Check(
                    "nic", "NCCL interface", FAIL,
                    f"interface {name} does not exist on {machine.name}",
                    "The group's NCCL_SOCKET_IFNAME / the machine's NCCL interface names "
                    "an interface this box does not have; NCCL fails at rendezvous.",
                ))
        elif head == "ib":
            count = int(rest[0]) if rest and rest[0].isdigit() else 0
            if count:
                checks.append(Check("ib", "InfiniBand/RoCE", PASS, f"{count} device(s)"))
            elif expect_ib:
                checks.append(Check(
                    "ib", "InfiniBand/RoCE", WARN,
                    f"no RDMA device on {machine.name}, but the group configures NCCL for one",
                    "Multi-node tensor/pipeline parallel over Ethernet is possible but "
                    "usually much slower; confirm this is intended.",
                ))
            else:
                checks.append(Check("ib", "InfiniBand/RoCE", SKIP, "no RDMA device",
                                    "Fine when the group does not ask for one."))

    kube = any(p[:2] == ["kube", "yes"] for p in seen)
    if kube and 30000 <= int(port) <= 32767:
        checks.append(Check(
            "port_range", "Port reachability", FAIL,
            f"{port} is inside the k8s NodePort range and {machine.name} runs kube-proxy",
            "kube-proxy hijacks 30000–32767 on the node IP, so the engine binds fine and "
            "is reachable only from localhost — it shows up as a readiness timeout.",
        ))
    return checks


def homogeneity_check(ranked: list[Any]) -> Check:
    """One group's members must agree on substrate, card type and card count.

    Re-checked at preflight rather than trusted from group-create time because a
    machine can be edited or re-probed from the cluster after it was grouped,
    and the engine's parallel split has no way to describe an uneven gang.
    """
    if not ranked:
        return Check("homogeneity", "Member parity", FAIL, "the group has no members")
    types = {getattr(m, "gpu_type", "") or "(unset)" for _, m in ranked}
    counts = {getattr(m, "gpu_count", 0) for _, m in ranked}
    drivers = {getattr(m, "driver", "") or "ssh_docker" for _, m in ranked}
    if len(types) == 1 and len(counts) == 1 and len(drivers) == 1:
        return Check(
            "homogeneity", "Member parity", PASS,
            f"{len(ranked)} members: one substrate, "
            f"{counts.pop()}×{types.pop()} each",
        )
    return Check(
        "homogeneity", "Member parity", FAIL,
        f"mixed members — substrates {sorted(drivers)}, card types {sorted(types)}, "
        f"card counts {sorted(counts)}",
        "Every member of a group must share a substrate, a GPU type and a card "
        "count; split the group or fix the machine.",
    )


def inspect_group(
    driver: Any,
    ranked: list[Any],
    *,
    image: str,
    model_path: str,
    volumes: dict[str, str],
    service_port: int,
    nccl_env: dict[str, str] | None = None,
    timeout: int = 45,
) -> list[dict]:
    """A group's preflight: the per-machine probes, plus the interconnect.

    `ranked` is `[(rank, MachineInfo)]` in rank order, master first. The master's
    interior address is what every worker must reach; the workers are the ones
    probed for it (the master does not dial them).

    Never raises — one unreachable member is a finding on that member's row and
    the rest of the group is still checked.
    """
    if not ranked:
        return []
    parity = homogeneity_check(ranked)
    nccl_env = nccl_env or {}
    ifname = nccl_env.get("NCCL_SOCKET_IFNAME", "")
    expect_ib = ifname.startswith("ib") or any(k.startswith("NCCL_IB") for k in nccl_env)
    master_host = getattr(ranked[0][1], "interior_host", None) or ranked[0][1].host

    out: list[dict] = []
    for rank, machine in ranked:
        member_ifname = ifname or getattr(machine, "nccl_ifname", "")
        checks = inspect(
            driver, machine,
            image=image, model_path=model_path, volumes=volumes,
            port=service_port,
            # The master is not told to reach itself; workers are.
            peer_host=master_host if rank > 0 else "",
            nccl_ifname=member_ifname,
            expect_ib=expect_ib,
            timeout=timeout,
        )
        checks.append(parity)
        out.append(Preflight(machine.name, checks).as_dict())
    return out


def parity_check(missing: list[dict], container: str) -> Check:
    """Flags production sets that this campaign does not.

    A warning, never a failure: running with engine defaults is a legitimate
    thing to test, and sometimes the point. But it is also how a config fails a
    benchmark check the baseline canary passed minutes earlier, so it should be
    a decision rather than a discovery.
    """
    if not missing:
        return Check("parity", "Matches production", PASS,
                     "every flag the production service sets is set here too")
    flags = ", ".join(
        f"{m['flag']}={m['production']}" if m["production"] is not True else m["flag"]
        for m in missing[:8]
    )
    more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
    return Check(
        "parity", "Matches production", WARN,
        f"{len(missing)} flag(s) {container or 'production'} sets that this campaign does "
        f"not: {flags}{more}",
        "Every candidate inherits the engine default for these. That is fine if it is "
        "deliberate — and is how a config fails a check the baseline canary just passed "
        "if it is not.",
    )


def benchmark_check(
    key: str,
    label: str,
    slug: str,
    catalog: dict[str, list[str]] | None,
    target_metric: str = "",
) -> Check:
    """Does this benchmark exist, and can it report what the objective ranks on?

    Both failures look identical afterwards: a run that completed, produced a
    full metric set, and scored None — reported in the morning as "nothing
    stayed inside the redlines". The second is the easier mistake to make now
    that a campaign carries two benchmarks with disjoint metric names, because
    the objective that is right for one is silently empty against the other.
    """
    if catalog is None:
        # The benchmark platform is unreachable or refused us. That is a fact
        # about right now, not about this campaign — never a blocker.
        return Check(key, label, SKIP, "could not reach the benchmark platform to check")
    if not slug:
        return Check(key, label, PASS, "the platform default")
    if slug not in catalog:
        # Fuzzy rather than substring: the typo that actually happens is a
        # wrong version suffix, and "…-v1" contains none of "…-v0".
        near = get_close_matches(slug, sorted(catalog), n=3, cutoff=0.5)
        return Check(
            key, label, FAIL, f"no benchmark named {slug} on the platform",
            f"Did you mean {', '.join(near)}?" if near
            else f"{len(catalog)} benchmark(s) are available to this account.",
        )
    modules = [m for m in catalog[slug] if m]
    module = target_metric.split(".")[0] if "." in target_metric else ""
    if module and module not in modules:
        return Check(
            key, label, FAIL,
            f"{slug} runs {', '.join(sorted(set(modules))) or 'no modules'}, so it never "
            f"reports {target_metric}",
            "Every run would complete and score nothing. Point the objective at a metric "
            "one of this benchmark's modules returns.",
        )
    return Check(key, label, PASS, f"{slug} runs {', '.join(sorted(set(modules))) or 'nothing'}")


def dataset_check(
    profile: str,
    verify_slug: str,
    wired: dict[str, str] | None,
    known_profiles: list[str] | None,
) -> Check:
    """Will the replay stage actually measure against the pinned dataset?

    Two ways to configure this into silence, both of which look fine until the
    morning. Pinning a profile the verify benchmark does not resolve means
    every run replays something else — the pin holds, nothing obeys it. And a
    benchmark that resolves a rolling profile with no pin at all is the
    original problem: a campaign spanning two nights can be measured with two
    different instruments and rank one against the other.
    """
    key, label = "dataset", "Replay dataset"
    if not verify_slug:
        return Check(key, label, PASS, "no replay stage")
    if wired is None:
        return Check(key, label, SKIP, "could not reach the benchmark platform to check")

    resolves = wired.get(verify_slug, "")
    if not profile:
        if resolves:
            return Check(
                key, label, WARN,
                f"{verify_slug} replays {resolves}, whichever build is current when each "
                "run submits",
                "A campaign spanning more than one night can be measured on two different "
                "builds. Pin the profile to hold one for its whole life.",
            )
        return Check(key, label, PASS, "no dataset profile to pin")

    if known_profiles is not None and profile not in known_profiles:
        near = get_close_matches(profile, sorted(known_profiles), n=3, cutoff=0.5)
        return Check(
            key, label, FAIL, f"no collection profile named {profile}",
            f"Did you mean {', '.join(near)}?" if near
            else f"{len(known_profiles)} profile(s) exist on the platform.",
        )
    if not resolves:
        return Check(
            key, label, FAIL,
            f"{verify_slug} does not resolve a collection profile, so pinning {profile} "
            "changes nothing",
            "Its replay module needs dataset_source: auto and dataset_profile set to "
            f"{profile}.",
        )
    if resolves != profile:
        return Check(
            key, label, FAIL,
            f"{verify_slug} replays {resolves}, not the pinned {profile}",
            "Every run would be measured against a dataset the campaign is not holding, "
            "and every result would be flagged as not comparable.",
        )
    return Check(key, label, PASS, f"{verify_slug} replays {profile}, pinned for this campaign")


def space_check(candidate_count: int, errors: list[str]) -> Check:
    if errors:
        return Check("space", "Search space", FAIL, "; ".join(errors))
    if candidate_count == 0:
        return Check("space", "Search space", FAIL, "expands to no candidates")
    return Check("space", "Search space", PASS,
                 f"{candidate_count} candidate(s) to try")


def summarize(machine_name: str, checks: list[Check]) -> dict:
    return Preflight(machine=machine_name, checks=checks).as_dict()


def as_json(checks: list[Check]) -> str:
    return json.dumps([c.as_dict() for c in checks], indent=2)
