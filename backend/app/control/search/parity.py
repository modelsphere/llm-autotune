"""What the campaign's config does NOT say that production's config does.

A campaign lost a night to this. Production runs with `--enable-cache-report`;
the campaign's base config did not set it, so sglang stopped reporting cached
tokens and one benchmark check failed on every candidate, for a reason that had
nothing to do with the parameters being swept.

The reference is the production configuration recorded on **Baselines** for
this model, engine and card type: an engine default is not the baseline, the
deployed config is.

Deliberately one-directional. A flag whose VALUE differs is the point of the
campaign (that is the sweep); a flag production sets and the campaign never
mentions is a silent inheritance of an engine default nobody chose.
"""

from typing import Any

from app.control.engine_command import PLACEMENT_FLAGS


def missing_flags(config: dict, production: dict | None) -> list[dict[str, Any]]:
    """Flags production's engine args set that this config never mentions.

    Returns [{"flag": "enable_cache_report", "production": True}].
    """
    missing = []
    for key, value in sorted((production or {}).items()):
        if key in PLACEMENT_FLAGS or key in config:
            continue
        missing.append({"flag": key, "production": value})
    return missing
