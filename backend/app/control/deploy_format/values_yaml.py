"""values_yaml — a Helm-values style file, read and edited line by line.

The shape of a per-model Helm values file: a few
top-level mappings holding the image, the model, the service, plus an
`extraArgs` list of engine flags and a k8s-style `env` list. Where each of
those lives, and how the args are spelled, are OPTIONS (dotted paths and a
style per section), so another repo with `sglang.args:` instead of
`extraArgs:` is a preset, not a fork of this file.

Why line-based and not parse→mutate→dump: the deploy repo's CI validates the
file with awk, line by line, and expects an exact top-level key set, two-space
indentation, one item per line. A YAML dumper would reorder, requote and drop
comments — a diff nobody asked for and a pipeline that may reject it. So the
file is read into regions and knob sites, and an edit rewrites, deletes or
appends single lines. Nothing the plan does not name is touched.

Arg styles:
  list-eq     - "--flag=value"        (one item per knob; the default)
  list-pairs  - --flag / - value      (read on any list; written for a two-line
                                       site only where the file already has one)
  mapping     flag: value             (a mapping of flag → value)
Env styles:  k8s-list (- name/value) | mapping.  Env is read, never written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.control.deploy_format.base import ChangePlan, DeployFormat, KnobSite, ParsedFile
from app.control.engine_command import PLACEMENT_FLAGS, parse_engine_args
from app.control.engines.flags import normalize_args, render_flag

# Flags the shared chart renders itself. The platform's LaunchSpec sets the
# same ones outside engine_args, so a winner never carries them — but a
# hand-written search-space base might, and they must not be appended where
# the chart's own copy would collide. Overridable per binding.
DEFAULT_CHART_OWNED = sorted(PLACEMENT_FLAGS | {"enable_metrics"})

_KEY = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.\-/]*):\s*(.*?)\s*$")
_ITEM = re.compile(r"^(\s*)-\s+(.*?)\s*$")
_COMMENT = re.compile(r"^\s*(#.*)?$")


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _quote_of(raw: str) -> str:
    return raw[0] if raw[:1] in ('"', "'") and len(raw) > 1 and raw.endswith(raw[0]) else ""


def _flag_to_key(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def fmt_value(value: Any) -> str:
    """A knob value as it is spelled in the file."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))  # 2.0 from a float-snapping policy is still 2
    return str(value)


@dataclass
class Region:
    """A `key:` and the lines under it, up to the next key at the same or a
    shallower indentation. `header` is the key's own line."""

    header: int
    start: int
    end: int  # exclusive
    indent: int  # the key's indentation, in characters


class ValuesYamlFormat(DeployFormat):
    name = "values_yaml"
    default_options: dict[str, Any] = {
        "args": {"path": "extraArgs", "style": "list-eq"},
        "image": {"repository": "image.repository", "tag": "image.tag", "digest": "image.digest"},
        "gpus": {"path": "model.gpus"},
        "model_path": {"path": "model.localPath"},
        "served_model_name": {"path": ""},
        "port": {"path": "service.port"},
        "env": {"path": "env", "style": "k8s-list"},
        "gpu_product": {"path": "affinity", "label": "nvidia.com/gpu.product"},
        "chart_owned_flags": DEFAULT_CHART_OWNED,
    }

    # -- regions -------------------------------------------------------------

    @staticmethod
    def _region(lines: list[str], dotted: str) -> Region | None:
        """Walk a dotted path through nested mappings by indentation."""
        if not dotted:
            return None

        def indent_of(line: str) -> int:
            return len(line) - len(line.lstrip(" "))

        def meaningful(line: str) -> bool:
            return bool(line.strip()) and not _COMMENT.match(line)

        lo, hi = 0, len(lines)
        region: Region | None = None
        for part in dotted.split("."):
            # The children of the current region all sit at one indentation:
            # that of its first meaningful line. Only keys at that depth are
            # candidates, so a same-named grandchild never matches.
            child_indent = next(
                (indent_of(lines[i]) for i in range(lo, hi) if meaningful(lines[i])), None
            )
            if child_indent is None:
                return None
            header = None
            for index in range(lo, hi):
                match = _KEY.match(lines[index])
                if (
                    match
                    and len(match.group(1)) == child_indent
                    and match.group(2) == part
                    and not _COMMENT.match(lines[index])
                ):
                    header = index
                    break
            if header is None:
                return None
            # The region runs to the next meaningful line at the key's depth
            # or shallower — except list items at the key's own depth, which
            # YAML allows (`key:` / `- item`).
            end = hi
            for index in range(header + 1, hi):
                line = lines[index]
                if not meaningful(line):
                    continue
                if indent_of(line) < child_indent or (
                    indent_of(line) == child_indent and not _ITEM.match(line)
                ):
                    end = index
                    break
            region = Region(header=header, start=header + 1, end=end, indent=child_indent)
            lo, hi = region.start, region.end
        return region

    def _scalar(self, lines: list[str], dotted: str) -> tuple[int | None, str, str]:
        """(line index, unquoted value, quote) of a scalar at a dotted path."""
        region = self._region(lines, dotted)
        if region is None:
            return None, "", ""
        match = _KEY.match(lines[region.header])
        raw = match.group(3) if match else ""
        return region.header, _unquote(raw), _quote_of(raw)

    # -- parse ---------------------------------------------------------------

    def parse(self, text: str, engine: str = "sglang") -> ParsedFile:
        lines = text.splitlines()
        parsed = ParsedFile()
        opts = self.options

        image = opts.get("image") or {}
        if image.get("ref"):
            _, ref, _ = self._scalar(lines, image["ref"])
            parsed.image = ref
            repo, _, tail = (
                ref.rpartition("@")
                if "@" in ref
                else (ref.rpartition(":")[0], "", ref.rpartition(":")[2])
            )
            if "@" in ref:
                parsed.image_repository, parsed.image_digest = repo, tail
            elif repo:
                parsed.image_repository, parsed.image_tag = repo, tail
            else:
                parsed.image_repository = ref
        else:
            _, parsed.image_repository, _ = self._scalar(lines, image.get("repository", ""))
            _, parsed.image_tag, _ = self._scalar(lines, image.get("tag", ""))
            _, parsed.image_digest, _ = self._scalar(lines, image.get("digest", ""))
            if parsed.image_digest:
                parsed.image = f"{parsed.image_repository}@{parsed.image_digest}"
            elif parsed.image_tag:
                parsed.image = f"{parsed.image_repository}:{parsed.image_tag}"
            else:
                parsed.image = parsed.image_repository

        _, parsed.model_path, _ = self._scalar(
            lines, (opts.get("model_path") or {}).get("path", "")
        )
        _, parsed.served_model_name, _ = self._scalar(
            lines, (opts.get("served_model_name") or {}).get("path", "")
        )
        _, port, _ = self._scalar(lines, (opts.get("port") or {}).get("path", ""))
        if port:
            try:
                parsed.service_port = int(port)
            except ValueError:
                parsed.warnings.append(f"service port is not an integer: {port!r}")
        _, gpus, _ = self._scalar(lines, (opts.get("gpus") or {}).get("path", ""))
        if gpus:
            try:
                parsed.gpus = int(gpus)
            except ValueError:
                parsed.warnings.append(f"gpus is not an integer: {gpus!r}")

        parsed.env = self._env(lines)
        parsed.gpu_product = self._gpu_product(lines)

        args_opts = opts.get("args") or {}
        region = self._region(lines, args_opts.get("path", ""))
        if region is None:
            parsed.warnings.append(f"no {args_opts.get('path', 'args')} section found")
            return parsed
        style = args_opts.get("style", "list-eq")
        sites = (
            self._mapping_sites(lines, region)
            if style == "mapping"
            else self._list_sites(lines, region)
        )

        def as_flag(site: KnobSite) -> str:
            return site.flag if site.flag.startswith("-") else "--" + site.flag.replace("_", "-")

        raw_args = parse_engine_args(
            [as_flag(s) if s.value is True else f"{as_flag(s)}={s.value}" for s in sites]
        )
        canonical, parsed.normalized = normalize_args(engine, raw_args)
        keys, _ = normalize_args(engine, {_flag_to_key(as_flag(s)): True for s in sites})
        for site, key in zip(sites, list(keys), strict=False):
            if key in parsed.sites:
                parsed.warnings.append(f"{site.flag} appears twice; the later one is used")
            parsed.sites[key] = site
        chart_owned = set(opts.get("chart_owned_flags") or [])
        for key, value in canonical.items():
            if key in chart_owned:
                parsed.chart_owned_present.append(key)
                continue
            parsed.engine_args[key] = value
        return parsed

    @staticmethod
    def _list_sites(lines: list[str], region: Region) -> list[KnobSite]:
        """Items in file order; a `--flag` item followed by a non-flag item is
        one two-line knob."""
        sites: list[KnobSite] = []
        for index in range(region.start, region.end):
            match = _ITEM.match(lines[index])
            if not match:
                continue
            indent, raw = match.group(1), match.group(2)
            quote = _quote_of(raw)
            text = _unquote(raw)
            if not text.startswith("-"):
                if sites and sites[-1].value is True and sites[-1].value_line is None:
                    sites[-1].value, sites[-1].value_line, sites[-1].value_quote = (
                        text,
                        index,
                        quote,
                    )
                continue
            flag, has_value, value = text.partition("=")
            sites.append(
                KnobSite(
                    flag=flag,
                    value=value if has_value else True,
                    line=index,
                    indent=indent,
                    quote=quote,
                )
            )
        return sites

    @staticmethod
    def _mapping_sites(lines: list[str], region: Region) -> list[KnobSite]:
        sites: list[KnobSite] = []
        for index in range(region.start, region.end):
            match = _KEY.match(lines[index])
            if not match or _COMMENT.match(lines[index]):
                continue
            indent, key, raw = match.group(1), match.group(2), match.group(3)
            value = _unquote(raw)
            if value.lower() in ("true", ""):
                value = True
            elif value.lower() == "false":
                continue  # an explicit off on a mapping: same as absent
            # The key as written — `tp-size` or `--tp-size` — so an edit keeps it.
            sites.append(
                KnobSite(flag=key, value=value, line=index, indent=indent, quote=_quote_of(raw))
            )
        return sites

    def _env(self, lines: list[str]) -> dict[str, str]:
        opts = self.options.get("env") or {}
        region = self._region(lines, opts.get("path", ""))
        out: dict[str, str] = {}
        if region is None:
            return out
        if opts.get("style") == "mapping":
            for index in range(region.start, region.end):
                match = _KEY.match(lines[index])
                if match and not _COMMENT.match(lines[index]):
                    out[match.group(2)] = _unquote(match.group(3))
            return out
        name = None
        for index in range(region.start, region.end):
            text = lines[index].strip()
            if text.startswith("- name:"):
                name = _unquote(text[len("- name:") :])
            elif text.startswith("value:") and name is not None:
                out[name] = _unquote(text[len("value:") :])
                name = None
        return out

    def _gpu_product(self, lines: list[str]) -> str:
        opts = self.options.get("gpu_product") or {}
        region = self._region(lines, opts.get("path", ""))
        label = opts.get("label", "")
        if region is None or not label:
            return ""
        seen = False
        for index in range(region.start, region.end):
            if label in lines[index]:
                seen = True
                continue
            if seen:
                match = _ITEM.match(lines[index])
                if match:
                    return _unquote(match.group(2))
        return ""

    # -- apply ---------------------------------------------------------------

    def apply(self, text: str, parsed: ParsedFile, plan: ChangePlan) -> str:
        lines = text.splitlines()
        trailing_newline = text.endswith("\n")
        opts = self.options
        args_opts = opts.get("args") or {}
        style = args_opts.get("style", "list-eq")
        region = self._region(lines, args_opts.get("path", ""))
        if region is None and plan.changes:
            raise ValueError(f"cannot apply engine-arg changes: no {args_opts.get('path')} section")

        sites = list(parsed.sites.values())
        item_indent = _dominant(s.indent for s in sites) or (
            " " * (region.indent + 2) if region else "  "
        )
        quote = _dominant(s.quote for s in sites) if sites else '"'

        edits: dict[int, str | None] = {}
        appended: list[str] = []

        def item(flag: str, value: Any, q: str, indent: str) -> str:
            if style == "mapping":
                body = fmt_value(value) if value is not True else "true"
                return f"{indent}{flag}: {q}{body}{q}"
            body = flag if value is True else f"{flag}={fmt_value(value)}"
            return f"{indent}- {q}{body}{q}"

        for change in plan.changes:
            site = parsed.sites.get(change.key)
            if change.kind in ("changed", "removed") and site is None:
                raise ValueError(f"change {change.key} names a knob the file does not carry")
            if change.kind == "removed" or (change.kind == "changed" and change.after is False):
                edits[site.line] = None
                if site.value_line is not None:
                    edits[site.value_line] = None
            elif change.kind == "changed":
                if site.value_line is not None:
                    q = site.value_quote
                    edits[site.value_line] = (
                        None
                        if change.after is True
                        else f"{site.indent}- {q}{fmt_value(change.after)}{q}"
                    )
                else:
                    edits[site.line] = item(site.flag, change.after, site.quote, site.indent)
            elif change.kind == "added":
                flag = change.flag or render_flag("sglang", change.key)
                if style == "mapping" and sites and not sites[0].flag.startswith("-"):
                    flag = flag.lstrip("-")
                appended.append(item(flag, change.after, quote, item_indent))

        if plan.gpus_after is not None:
            index, _, q = self._scalar(lines, (opts.get("gpus") or {}).get("path", ""))
            if index is None:
                raise ValueError("cannot update gpus: no such line")
            edits[index] = self._rewrite_scalar(lines[index], str(plan.gpus_after), q or '"')
        if plan.image_tag_after:
            image = opts.get("image") or {}
            if image.get("ref"):
                index, ref, q = self._scalar(lines, image["ref"])
                new_ref = f"{parsed.image_repository}:{plan.image_tag_after}"
            else:
                index, _, q = self._scalar(lines, image.get("tag", ""))
                new_ref = plan.image_tag_after
            if index is None:
                raise ValueError("cannot update image tag: no such line")
            edits[index] = self._rewrite_scalar(lines[index], new_ref, q)
        if plan.model_path_after:
            index, _, q = self._scalar(lines, (opts.get("model_path") or {}).get("path", ""))
            if index is None:
                raise ValueError("cannot update model path: no such line")
            edits[index] = self._rewrite_scalar(lines[index], plan.model_path_after, q)

        insert_at = None
        if appended and region is not None:
            last = max((s.value_line or s.line for s in sites), default=region.header)
            insert_at = last + 1

        out: list[str] = []
        for index, line in enumerate(lines):
            if insert_at == index:
                out.extend(appended)
            if index in edits:
                if edits[index] is not None:
                    out.append(edits[index])  # type: ignore[arg-type]
                continue
            out.append(line)
        if insert_at is not None and insert_at >= len(lines):
            out.extend(appended)
        result = "\n".join(out)
        return result + "\n" if trailing_newline else result

    @staticmethod
    def _rewrite_scalar(line: str, value: str, quote: str) -> str:
        match = _KEY.match(line)
        assert match is not None
        return f"{match.group(1)}{match.group(2)}: {quote}{value}{quote}"


def _dominant(values) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
