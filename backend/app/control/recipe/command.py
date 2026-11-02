"""The engine flags a `commandOverride` passes, read best-effort.

A recipe whose chart cannot express how its engine spans the nodes (vLLM's
data-parallel engine across a LeaderWorkerSet, for one) writes the whole
command as a script under `commandOverride`, and the chart then takes no
`extraArgs`. The flags are still in there, as shell:

    set -- /models \\
      --tensor-parallel-size 8 \\
      --served-model-name "${SERVED_MODEL_NAME}"
    if [ "$RANK" = 1 ]; then
      set -- "$@" --headless --data-parallel-start-rank 1
    fi
    exec vllm serve "$@" --data-parallel-address "$LEADER_IP"

This reads them for showing and comparing, never for writing back: editing a
person's script is not something a tuner can do safely, so a recipe like this
runs as written. Lines inside an `if` or `case` are flags only some pods get
(a rank's own), kept apart. A value the shell fills in at run time
(`$LEADER_IP`) is kept as written.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from app.control.recipe import args as argv
from app.control.recipe.model import Recipe

_OPENS = {"if": "fi", "case": "esac"}
# Words that start a command inside a branch without being it.
_LEADS = ("then", "else", "elif", "do")
_ENGINE_COMMAND = re.compile(
    r"(?:^|\s)(vllm\s+serve|sglang\s+serve|sglang\.launch_server|set\s+--)(?:\s|$)"
)


@dataclass
class CommandFlags:
    # Flags every pod runs with.
    flags: dict[str, Any] = field(default_factory=dict)
    # Flags inside an if/case: some pods only.
    conditional: dict[str, Any] = field(default_factory=dict)


def _script(override: Any) -> str:
    """The shell text of `commandOverride`: the argument of `sh -c`/`bash -c`,
    or the argv itself, quoted back into one line."""
    if isinstance(override, str):
        return override
    items = [str(i) for i in override or []]
    if len(items) >= 3 and items[0].rsplit("/", 1)[-1] in ("sh", "bash") and items[1] == "-c":
        return items[2]
    return shlex.join(items)


def command_flags(recipe: Recipe) -> CommandFlags:
    out = CommandFlags()
    override = recipe.values.get("commandOverride")
    if override is None:
        return out
    text = re.sub(r"\\\n", " ", _script(override))
    depth = 0
    for line in text.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        # Branches open and close on one line as often as on several
        # (`case "$R" in 0|1) ;; esac`, `if x; then set -- "$@" --y; fi`):
        # walk the line command by command.
        for command in _commands(words):
            head = command[0]
            if head in _OPENS:
                depth += 1
                continue
            if head in _OPENS.values():
                depth = max(depth - 1, 0)
                continue
            while command and command[0] in _LEADS:
                command = command[1:]
            if not command or not _ENGINE_COMMAND.search(" ".join(command)):
                continue
            found = argv.flags(recipe.engine, [w for w in command if w != "$@"])
            found.pop("", None)  # `set --` and the positional after it
            (out.conditional if depth else out.flags).update(found)
    return out


def _commands(words: list[str]) -> list[list[str]]:
    """A line's words, cut into commands at `;`, `&&` and `||`."""
    out: list[list[str]] = [[]]
    for word in words:
        if word in (";", ";;", "&&", "||"):
            out.append([])
            continue
        ends = word.endswith(";") and not word.endswith("\\;")
        word = word.rstrip(";")
        if word:
            out[-1].append(word)
        if ends:
            out.append([])
    return [c for c in out if c]


def recipe_flags(recipe: Recipe) -> dict[str, Any]:
    """The engine flags a recipe runs with, as every part of the platform
    reads them: `extraArgs`, or, when a `commandOverride` script replaces the
    chart's command, the flags the script passes to every pod (read, never
    written back)."""
    if recipe.values.get("commandOverride") is not None:
        return dict(command_flags(recipe).flags)
    return recipe.flags()
