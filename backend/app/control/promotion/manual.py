"""Manual promotion — the default, and the one that always works.

No external system. The platform's contribution ends where the winner's exact
config is rendered into an artifact a human (or a gitops repo) can apply: the
pinned image, the engine command, the tuned args. It is the honest default
while the GitLab + A/B-test path is still TBD — the config is *exposed*, and a
person takes it from there.

`open_rollout` therefore leaves the rollout SUBMITTED and does not pretend to
know when a human finished applying it; `status()` stays put. Nothing here can
fail for infrastructure reasons, which is exactly why it is the fallback.
"""

from __future__ import annotations

import json

from app.control.promotion.base import (
    PromotionHandle,
    PromotionRequest,
    PromotionState,
    PromotionTarget,
)


class ManualPromotionTarget(PromotionTarget):
    name = "manual"

    def open_rollout(self, request: PromotionRequest) -> PromotionHandle:
        config = request.config or {}
        command = config.get("command") or []
        image = (config.get("image") or {}).get("ref", "")
        return PromotionHandle(
            target=self.name,
            state=PromotionState.SUBMITTED,
            refs={
                # The artifact to apply: the whole config payload, plus the two
                # things a human reaches for first — what image, what command.
                "artifact": json.dumps(config, indent=2, sort_keys=True),
                "image": image,
                "command": " ".join(str(part) for part in command),
            },
            detail=(
                "config rendered for manual rollout — apply the recorded command/image "
                "to the serving cluster; the platform does not roll it out itself"
            ),
        )
