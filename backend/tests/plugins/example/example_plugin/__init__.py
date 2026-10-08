"""The example plugin: one of each hook, small enough to read in one sitting.

The platform's CI installs it and runs tests/plugins/ against it, so a change
to the platform that would break a plugin breaks this one first. To start a
plugin of your own, copy this directory and rename it.

It keeps a note of how many campaigns were active on every worker tick, lets
people put a label on a campaign, plans the campaigns labelled
`plan:reverse`, and holds one setting.
"""

from pathlib import Path

from app.plugin_api import PLUGIN_API_VERSION, Plugin

from example_plugin.api import router
from example_plugin.planner import plan_reverse
from example_plugin.seed import seed
from example_plugin.tick import note_active_campaigns

plugin = Plugin(
    name="example",
    api_version=PLUGIN_API_VERSION,
    routers=(router,),
    tick_steps=(note_active_campaigns,),
    migrations=str(Path(__file__).parent / "migrations"),
    on_bootstrap=seed,
    propose_candidates=plan_reverse,
)
