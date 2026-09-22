"""Ownership policy and equivalences — what a difference between the
platform's config and the deploy file MEANS, decided by a person, once.

The platform cannot make the repo spell things its way, and must not overwrite
what it does not understand. So every field and every knob has an owner:

    platform   the platform tracks it: adopted on sync, proposed in a merge
               request (the tuning knobs; gpus)
    repo       the repo keeps its own value: never written, every difference
               REPORTED (image, model path, env, volumes by default)
    ignore     never written, never reported — an explicit decision
    substrate  not configuration at all (port, served name, chart-owned
               flags): silently the same across substrates, not settable

and some pairs of values are declared to mean the same thing on the two sides
(a weights path on our boxes ↔ on the prod node; a mirror image tag; a draft
model mounted elsewhere). Equivalences translate in both directions, so the
sync sees no drift and the merge request writes the repo's spelling.

Path-valued knobs are the one heuristic: a knob whose value looks like a
filesystem path on either side is treated as `repo` until someone says
otherwise, because adopting `/draft-model` into a baseline that launches on
bare metal breaks the launch. Nothing else is guessed.

The shortcuts are the common decisions as one click: ignore image, ignore
model path, ignore env, ignore volumes, ignore the parser flags, ignore
path-valued flags, follow image, follow model path.
"""

from __future__ import annotations

import re
from typing import Any

from app.control.deploy_format.base import ChangePlan, KnobChange, ParsedFile
from app.control.engines.flags import normalize_args, render_flag
from app.control.launch_config import IDENTITY_FIELDS, SUBSTRATE_FIELDS, LaunchConfig
from app.control.search.validation import cards_used

CLASSES = ("platform", "repo", "ignore")
SUBSTRATE = "substrate"

# Fields the policy can speak about. `gpus` is the file's GPUs-per-replica,
# derived from the knobs on the platform side.
POLICY_FIELDS: tuple[str, ...] = (*IDENTITY_FIELDS, "gpus")

DEFAULT_FIELD_POLICY: dict[str, str] = {
    "image": "repo",
    "model_path": "repo",
    "extra_env": "repo",
    "extra_volumes": "repo",
    "gpus": "platform",
}
DEFAULT_PATH_KNOBS = "repo"

PARSER_KNOBS = ("reasoning_parser", "tool_call_parser", "chat_template", "tokenizer_path")

SHORTCUTS: dict[str, dict[str, Any]] = {
    "ignore_image": {
        "label": "Ignore image",
        "fields": {"image": "ignore"},
        "hint": "The repo's image tag is its own business; never write or report it.",
    },
    "ignore_model_path": {
        "label": "Ignore model path",
        "fields": {"model_path": "ignore"},
        "hint": "Weights live at a different path on the prod node; never write or report it.",
    },
    "ignore_env": {
        "label": "Ignore env",
        "fields": {"extra_env": "ignore"},
        "hint": "Env vars differ per substrate; never report them.",
    },
    "ignore_volumes": {
        "label": "Ignore volumes",
        "fields": {"extra_volumes": "ignore"},
        "hint": "Mounts differ per substrate; never report them.",
    },
    "ignore_gpus": {
        "label": "Ignore gpus",
        "fields": {"gpus": "ignore"},
        "hint": "Leave GPUs-per-replica to the repo even when the card count changes.",
    },
    "ignore_parsers": {
        "label": "Ignore parser flags",
        "knobs": {k: "ignore" for k in PARSER_KNOBS},
        "hint": "reasoning/tool-call parser and chat template are the repo's; never write them.",
    },
    "ignore_paths": {
        "label": "Ignore path-valued flags",
        "path_knobs": "ignore",
        "hint": "Any flag whose value is a filesystem path (draft model, tokenizer) is the repo's.",
    },
    "report_paths": {
        "label": "Report path-valued flags",
        "path_knobs": "repo",
        "hint": "Path-valued flags are the repo's, but a difference is still shown (the default).",
    },
    "follow_image": {
        "label": "Follow image",
        "fields": {"image": "platform"},
        "hint": "Propose the winner's image tag when it differs (same repository only).",
    },
    "follow_model_path": {
        "label": "Follow model path",
        "fields": {"model_path": "platform"},
        "hint": "Propose the winner's weights path when it differs.",
    },
}

_PATHLIKE = re.compile(r"^(/|\./|~/|[A-Za-z]:\\)")


def looks_like_path(value: Any) -> bool:
    return isinstance(value, str) and bool(_PATHLIKE.match(value.strip()))


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def apply_shortcut(policy: dict[str, Any], name: str) -> dict[str, Any]:
    """The policy with a shortcut folded in. Unknown names raise."""
    spec = SHORTCUTS.get(name)
    if spec is None:
        raise ValueError(f"unknown shortcut {name!r} (available: {', '.join(SHORTCUTS)})")
    out = {"fields": dict(policy.get("fields") or {}), "knobs": dict(policy.get("knobs") or {})}
    if policy.get("path_knobs"):
        out["path_knobs"] = policy["path_knobs"]
    out["fields"].update(spec.get("fields") or {})
    out["knobs"].update(spec.get("knobs") or {})
    if spec.get("path_knobs"):
        out["path_knobs"] = spec["path_knobs"]
    return out


def set_policy(
    policy: dict[str, Any], *, field: str = "", knob: str = "", owner: str = ""
) -> dict[str, Any]:
    if owner not in CLASSES:
        raise ValueError(f"owner must be one of {CLASSES}")
    out = {"fields": dict(policy.get("fields") or {}), "knobs": dict(policy.get("knobs") or {})}
    if policy.get("path_knobs"):
        out["path_knobs"] = policy["path_knobs"]
    if field:
        if field not in POLICY_FIELDS:
            raise ValueError(f"field must be one of {POLICY_FIELDS}")
        out["fields"][field] = owner
    elif knob:
        out["knobs"][knob] = owner
    else:
        raise ValueError("name a field or a knob")
    return out


def field_owner(policy: dict[str, Any], field: str) -> tuple[str, bool]:
    """(class, explicit) for a LaunchConfig field."""
    if field in SUBSTRATE_FIELDS:
        return SUBSTRATE, True
    fields = policy.get("fields") or {}
    if field in fields:
        return fields[field], True
    return DEFAULT_FIELD_POLICY.get(field, "repo"), False


def knob_owner(
    policy: dict[str, Any],
    knob: str,
    *,
    chart_owned: set[str] | frozenset[str] = frozenset(),
    ours: Any = None,
    theirs: Any = None,
) -> tuple[str, bool]:
    """(class, explicit) for an engine knob."""
    if knob in chart_owned:
        return SUBSTRATE, True
    knobs = policy.get("knobs") or {}
    if knob in knobs:
        return knobs[knob], True
    if looks_like_path(ours) or looks_like_path(theirs):
        return policy.get("path_knobs") or DEFAULT_PATH_KNOBS, bool(policy.get("path_knobs"))
    return "platform", False


# ---------------------------------------------------------------------------
# equivalences
# ---------------------------------------------------------------------------


def _pairs(equivalences: dict[str, Any], field: str, knob: str = "") -> list[list[str]]:
    if knob:
        return list((equivalences.get("knobs") or {}).get(knob) or [])
    return list(equivalences.get(field) or [])


def add_equivalence(
    equivalences: dict[str, Any], *, field: str = "", knob: str = "", ours: Any, theirs: Any
) -> dict[str, Any]:
    out = {
        k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in (equivalences or {}).items()
    }
    pair = [str(ours), str(theirs)]
    if knob:
        knobs = dict(out.get("knobs") or {})
        pairs = [list(p) for p in knobs.get(knob) or []]
        if pair not in pairs:
            pairs.append(pair)
        knobs[knob] = pairs
        out["knobs"] = knobs
    else:
        pairs = [list(p) for p in out.get(field) or []]
        if pair not in pairs:
            pairs.append(pair)
        out[field] = pairs
    return out


def to_theirs(equivalences: dict[str, Any], value: Any, *, field: str = "", knob: str = "") -> Any:
    for ours, theirs in _pairs(equivalences, field, knob):
        if str(value) == ours:
            return theirs
    return value


def to_ours(equivalences: dict[str, Any], value: Any, *, field: str = "", knob: str = "") -> Any:
    for ours, theirs in _pairs(equivalences, field, knob):
        if str(value) == theirs:
            return ours
    return value


def same_value(a: Any, b: Any) -> bool:
    """`"2"` and `2`, `0.85` and `"0.850"` are one value; booleans only
    equal booleans; dicts compare as dicts."""
    if isinstance(a, dict) or isinstance(b, dict):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None:
        return a is b
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def equivalent(
    equivalences: dict[str, Any], ours: Any, theirs: Any, *, field: str = "", knob: str = ""
) -> bool:
    if same_value(ours, theirs):
        return True
    return same_value(to_theirs(equivalences, ours, field=field, knob=knob), theirs) or same_value(
        ours, to_ours(equivalences, theirs, field=field, knob=knob)
    )


# ---------------------------------------------------------------------------
# divergences (sync direction: repo → platform)
# ---------------------------------------------------------------------------


def _image_tag(image: str) -> tuple[str, str]:
    """(repository, tag) of a tag reference; ("", "") for a digest reference."""
    if not image or "@" in image:
        return "", ""
    repo, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        return image, ""
    return repo, tag


def compare(
    ours: LaunchConfig,
    theirs: LaunchConfig,
    parsed: ParsedFile,
    policy: dict[str, Any],
    equivalences: dict[str, Any],
    *,
    chart_owned: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every difference between the platform's config and the file, sorted by
    who owns it: (to_adopt, divergences).

    `to_adopt` are platform-owned differences the sync applies to the row.
    `divergences` are everything else worth a decision: repo-owned (reported)
    and — when the owner was never set explicitly — unresolved.
    """
    adopt: list[dict[str, Any]] = []
    divergences: list[dict[str, Any]] = []

    def row(kind: str, key: str, o: Any, t: Any, owner: str, explicit: bool) -> dict[str, Any]:
        return {
            "kind": kind,
            "key": key,
            "ours": o,
            "theirs": t,
            "owner": owner,
            "status": owner if explicit else "unresolved",
        }

    keys = sorted(set(ours.engine_args) | set(theirs.engine_args))
    for key in keys:
        o, t = ours.engine_args.get(key), theirs.engine_args.get(key)
        owner, explicit = knob_owner(policy, key, chart_owned=chart_owned, ours=o, theirs=t)
        if owner in (SUBSTRATE, "ignore"):
            continue
        if equivalent(equivalences, o, t, knob=key):
            continue
        if owner == "platform":
            adopt.append(row("knob", key, o, t, owner, True))
        else:
            divergences.append(row("knob", key, o, t, owner, explicit))

    for field in IDENTITY_FIELDS:
        owner, explicit = field_owner(policy, field)
        if owner in (SUBSTRATE, "ignore"):
            continue
        o, t = getattr(ours, field), getattr(theirs, field)
        if field == "extra_volumes" and not t:
            continue  # the file's volumes are k8s objects; not compared
        if equivalent(equivalences, o, t, field=field):
            continue
        if owner == "platform" or (not o and t):
            # Platform-owned, or a blank on our side: filling a blank is not
            # overwriting anything, whoever owns the field.
            adopt.append(row("field", field, o, t, owner, True))
        else:
            divergences.append(row("field", field, o, t, owner, explicit))

    owner, explicit = field_owner(policy, "gpus")
    if parsed.gpus is not None and owner not in (SUBSTRATE, "ignore"):
        if parsed.gpus != ours.cards:
            entry = row("field", "gpus", ours.cards, parsed.gpus, owner, explicit)
            (adopt if owner == "platform" else divergences).append(entry)
    return adopt, divergences


def merge_divergences(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Carry a decision forward when the same difference is found again."""
    decided = {
        (d.get("kind"), d.get("key"), str(d.get("ours")), str(d.get("theirs"))): d
        for d in previous
        if d.get("status") != "unresolved"
    }
    out = []
    for d in current:
        key = (d.get("kind"), d.get("key"), str(d.get("ours")), str(d.get("theirs")))
        if key in decided and d["status"] == "unresolved":
            d = {
                **d,
                "status": decided[key]["status"],
                "decided_by": decided[key].get("decided_by", ""),
                "decided_at": decided[key].get("decided_at", ""),
            }
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# planning (merge-request direction: platform → repo)
# ---------------------------------------------------------------------------


def plan_changes(
    parsed: ParsedFile,
    target: LaunchConfig,
    *,
    policy: dict[str, Any] | None = None,
    equivalences: dict[str, Any] | None = None,
    chart_owned: set[str] | frozenset[str] = frozenset(),
    apply_removals: bool = False,
    promote_fields: set[str] | frozenset[str] = frozenset(),
    engine: str = "sglang",
) -> ChangePlan:
    """The knob-level difference between the file and the target config,
    filtered through the policy. `promote_fields` treats the named fields as
    platform-owned for this one plan (the "also update image.tag" tick)."""
    policy = policy or {}
    equivalences = equivalences or {}
    plan = ChangePlan()
    winner, _ = normalize_args(engine, dict(target.engine_args or {}))
    current = parsed.engine_args

    for key, value in winner.items():
        o, t = value, current.get(key)
        owner, _ = knob_owner(policy, key, chart_owned=chart_owned, ours=o, theirs=t)
        site = parsed.sites.get(key)
        flag = site.flag if site else render_flag(engine, key)
        if equivalent(equivalences, o, t, knob=key):
            continue
        if key in current:
            change = KnobChange(
                key=key,
                kind="changed",
                flag=flag,
                before=t,
                after=to_theirs(equivalences, o, knob=key),
            )
        elif value is False:
            continue  # off, and already absent
        else:
            change = KnobChange(
                key=key,
                kind="added",
                flag=flag,
                before=None,
                after=to_theirs(equivalences, o, knob=key),
            )
        if owner == "platform":
            plan.changes.append(change)
        elif owner == "repo":
            change.note = "repo-owned"
            plan.reported.append(change)
        else:
            change.note = owner
            plan.ignored.append(change)

    for key, value in current.items():
        if key in winner:
            continue
        owner, _ = knob_owner(policy, key, chart_owned=chart_owned, ours=None, theirs=value)
        if owner != "platform":
            continue
        site = parsed.sites.get(key)
        change = KnobChange(
            key=key,
            kind="removed",
            flag=site.flag if site else render_flag(engine, key),
            before=value,
            after=None,
        )
        (plan.changes if apply_removals else plan.kept).append(change)

    # gpus: follow the card count only when the file agrees with its own args.
    owner, _ = field_owner(policy, "gpus")
    if "gpus" in promote_fields:
        owner = "platform"
    winner_cards = cards_used(winner)
    head_cards = cards_used(current) if current else None
    if (
        parsed.gpus is not None
        and winner_cards != parsed.gpus
        and owner not in ("ignore", SUBSTRATE)
    ):
        if owner != "platform":
            plan.reported.append(
                KnobChange(
                    key="gpus",
                    kind="changed",
                    flag="gpus",
                    before=parsed.gpus,
                    after=winner_cards,
                    note="repo-owned",
                )
            )
        elif head_cards == parsed.gpus:
            plan.gpus_before, plan.gpus_after = parsed.gpus, winner_cards
        else:
            plan.warnings.append(
                f"the target uses {winner_cards} card(s) but the file's gpus={parsed.gpus} does "
                f"not equal what its own args imply ({head_cards}); gpus left as is — check the "
                "replica layout by hand"
            )

    # image: a tag change within the same repository, or a report.
    owner, _ = field_owner(policy, "image")
    if "image" in promote_fields:
        owner = "platform"
    ours_image = to_theirs(equivalences, target.image, field="image") if target.image else ""
    if (
        ours_image
        and parsed.image
        and owner not in ("ignore", SUBSTRATE)
        and not equivalent(equivalences, target.image, parsed.image, field="image")
    ):
        repo, tag = _image_tag(str(ours_image))
        if owner != "platform":
            plan.reported.append(
                KnobChange(
                    key="image",
                    kind="changed",
                    flag="image",
                    before=parsed.image,
                    after=ours_image,
                    note="repo-owned",
                )
            )
        elif tag and parsed.image_tag and repo == parsed.image_repository:
            plan.image_tag_before, plan.image_tag_after = parsed.image_tag, tag
        elif tag and repo != parsed.image_repository:
            plan.warnings.append(
                f"the target ran {ours_image} but the file pulls from {parsed.image_repository}; "
                "image left as is"
            )
        else:
            plan.warnings.append(
                f"cannot write image {ours_image!r} into the file's image fields; left as is"
            )

    # model path: written only when explicitly platform-owned.
    owner, _ = field_owner(policy, "model_path")
    if "model_path" in promote_fields:
        owner = "platform"
    if (
        target.model_path
        and parsed.model_path
        and owner not in ("ignore", SUBSTRATE)
        and not equivalent(equivalences, target.model_path, parsed.model_path, field="model_path")
    ):
        theirs_path = to_theirs(equivalences, target.model_path, field="model_path")
        if owner == "platform":
            plan.model_path_before, plan.model_path_after = parsed.model_path, str(theirs_path)
        else:
            plan.reported.append(
                KnobChange(
                    key="model_path",
                    kind="changed",
                    flag="model_path",
                    before=parsed.model_path,
                    after=theirs_path,
                    note="repo-owned",
                )
            )

    # env: compared, never edited.
    owner, _ = field_owner(policy, "extra_env")
    if owner not in ("ignore", SUBSTRATE):
        for name, value in (target.extra_env or {}).items():
            if parsed.env.get(name) != value and not equivalent(
                equivalences, value, parsed.env.get(name), field="extra_env"
            ):
                plan.reported.append(
                    KnobChange(
                        key=f"env:{name}",
                        kind="changed",
                        flag=name,
                        before=parsed.env.get(name),
                        after=value,
                        note="env is never edited by the platform",
                    )
                )
    return plan
