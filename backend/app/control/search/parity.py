"""What the campaign's config does NOT say that production's command does.

Campaign 19 lost a night to this. Production runs with `--enable-cache-report`;
the campaign's base config did not set it, so sglang stopped reporting cached
tokens and the benchmark's `d06_cache_hit` check failed — one check out of 51,
on every candidate, for a reason that had nothing to do with the parameters
being swept. The baseline canary passed the same benchmark minutes earlier,
because it measured production, which had the flag.

The platform already captures production's exact command to make teardown
reversible. The same capture answers "how does what we are about to launch
differ from what is serving today", which is the only reference that matters:
an engine default is not the baseline, the deployed config is.

Deliberately one-directional. A flag whose VALUE differs is the point of the
campaign (that is the sweep); a flag production sets and the campaign never
mentions is a silent inheritance of an engine default nobody chose.
"""

from typing import Any

from app.control.engine_command import PLACEMENT_FLAGS, parse_engine_args


def production_service(baseline: dict | None, served_model_name: str = "") -> dict:
    """The captured service to compare against — the one serving the same model
    where we can tell, otherwise the first."""
    services = (baseline or {}).get("services") or []
    if not services:
        return {}
    match = next(
        (s for s in services if s.get("served_model_name") == served_model_name), None
    )
    return match or services[0]


def missing_flags(
    config: dict, baseline: dict | None, served_model_name: str = ""
) -> list[dict[str, Any]]:
    """Flags production passes that this config never mentions.

    Returns [{"flag": "enable_cache_report", "production": True, "container": ...}].
    """
    service = production_service(baseline, served_model_name)
    if not service:
        return []
    # The unified config captured at inventory time; fall back to parsing the
    # raw command for services captured before it was stored.
    produced = service.get("engine_args") or parse_engine_args(
        service.get("command") or service.get("docker_run")
    )
    missing = []
    for key, value in sorted(produced.items()):
        if key in PLACEMENT_FLAGS or key in config:
            continue
        missing.append(
            {"flag": key, "production": value, "container": service.get("container", "")}
        )
    return missing
