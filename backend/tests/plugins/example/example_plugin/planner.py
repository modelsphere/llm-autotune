"""The example plugin's planner.

A campaign labelled `plan:reverse` tries the points of its space
last-declared first, no more at a time than its machines could start. Every
other campaign is left to the platform (None).
"""

from app.plugin_api import CandidateConfig, PlanContext, expand

from example_plugin.models import CampaignLabel


def plan_reverse(ctx: PlanContext) -> list[CandidateConfig] | None:
    label = ctx.session.get(CampaignLabel, ctx.campaign.id)
    if label is None or label.label != "plan:reverse":
        return None
    tried = {CandidateConfig(record.config).hash for record in ctx.history()}
    budget = max(ctx.startable_slots() - ctx.queued(), 0)
    points = [CandidateConfig(config) for config in reversed(expand(ctx.campaign.search_space))]
    return [point for point in points if point.hash not in tried][:budget]
