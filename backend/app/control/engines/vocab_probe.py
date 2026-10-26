"""Read an engine image's vocabulary from inside the image: every command-line
flag its server parser accepts and every environment variable it reads.

This file is not imported by the platform. It is shipped as text and run by
the image's own Python (`python3 -c …`), so it may use the standard library
only, and it must work across engine versions:

- environment variables are read from the engine's SOURCE with `ast` — the
  registry's type, default, choices, the comment above each entry and the
  section it sits in — without importing the engine or calling a getter
  (some create directories or probe hardware);
- flags come from the engine's own argparse parser, the one `serve` builds,
  which does mean importing it (and torch); a failure there still returns the
  env half, with a note.

Prints one line, MARKER + base64(zlib(json)), so whatever the engine logs
while importing cannot corrupt the result.
"""

import argparse
import ast
import base64
import importlib.util
import json
import os
import re
import sys
import zlib

MARKER = "@@AUTOTUNE_VOCAB@@"
# Directories of an engine package that are not the LLM server.
SKIP_DIRS = {"test", "tests", "multimodal_gen", "benchmark", "benchmarks", "examples"}


def package_dir(name):
    """The package's directory, found without importing it."""
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(f"package {name} not found")
    return list(spec.submodule_search_locations)[0]


def literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return "(computed)"


def comment_above(lines, lineno):
    """The comment block directly above a 1-based line, and the section header
    (a comment between two `# ====` rules) above that."""
    out = []
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        text = lines[i].strip().lstrip("#").strip()
        if set(text) <= set("=-") or re.match(r"^=+.*=+$", text):
            break
        out.append(text)
        i -= 1
    section = ""
    for j in range(lineno - 2, -1, -1):
        text = lines[j].strip()
        m = re.match(r"^#\s*=+\s*(.*?)\s*=*\s*$", text)
        if m and m.group(1):
            section = m.group(1)
            break
        if (text.startswith("# ===") and j > 0 and lines[j - 1].strip().startswith("#")
                and not set(lines[j - 1].strip().lstrip("#").strip()) <= set("=")):
            section = lines[j - 1].strip().lstrip("#").strip()
            break
    return " ".join(reversed(out)), section


# -- sglang ------------------------------------------------------------------


SGLANG_TYPES = {
    "EnvBool": "bool", "EnvBoolWithAlias": "bool", "EnvInt": "int", "EnvIntWithAlias": "int",
    "EnvFloat": "float", "EnvStr": "str", "EnvTuple": "list", "EnvJSON": "json",
}


def sglang_env(root, notes):
    path = os.path.join(root, "srt", "environ.py")
    source = open(path, encoding="utf-8").read()
    lines = source.splitlines()
    tree = ast.parse(source)
    envs = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Envs")
    entries, declared = [], set()
    for node in envs.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id.startswith("Env")):
            continue
        name = node.targets[0].id
        call = node.value
        kw = {k.arg: k.value for k in call.keywords}
        default = literal(call.args[0]) if call.args else literal(kw.get("default"))
        help_text, section = comment_above(lines, node.lineno)
        declared.add(name)
        alias = literal(kw["deprecated_name"]) if "deprecated_name" in kw else ""
        entries.append({
            "kind": "env", "name": name,
            "type": SGLANG_TYPES.get(call.func.id, call.func.id),
            "default": default, "help": help_text, "section": section,
            "secret": bool(literal(kw["secret"])) if "secret" in kw else False,
            "deprecated_alias": alias if isinstance(alias, str) else "",
            "where": f"srt/environ.py:{node.lineno}",
        })
    # Names read straight from os.environ elsewhere in the server.
    read = re.compile(
        r"""(?:os\.environ\.get|os\.getenv|get_bool_env_var|get_int_env_var|"""
        r"""get_float_env_var|os\.environ\[)\(?\s*["']((?:SGLANG|SGL)_[A-Z0-9_]+)["']"""
    )
    # A read whose neighbourhood says the variable is gone is a removal
    # notice, not a switch ("X has been removed: … always runs v2").
    retired = re.compile(
        r"has been removed|is removed|no longer (?:used|supported|has any effect)|is deprecated",
        re.I,
    )
    extra, gone = {}, set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                text = open(full, encoding="utf-8").read().splitlines()
            except Exception:
                continue
            for i, line in enumerate(text):
                for name in read.findall(line):
                    if name in declared or name in extra:
                        continue
                    lo, hi = max(0, i - 3), min(len(text), i + 4)
                    if any(name in ln and retired.search(ln) for ln in text[i:i + 8]):
                        gone.add(name)
                        continue
                    extra[name] = {
                        "kind": "env", "name": name, "type": "", "default": None,
                        "help": "\n".join(text[lo:hi]), "help_source": "source",
                        "section": "", "where": f"{os.path.relpath(full, root)}:{i + 1}",
                    }
    for name in gone - set(extra):
        notes.append(f"{name}: read only to say it was removed; left out")
    notes.append(f"{len(entries)} env vars from the Envs registry, {len(extra)} read elsewhere")
    return entries + sorted(extra.values(), key=lambda e: e["name"])


def sglang_open_choices(flags, notes):
    """Choices sglang keeps in a registry rather than on the parser: the
    speculative algorithms (an enum, plus aliases such as NEXTN its help names,
    plus any a plugin registers — so the list is marked open)."""
    for flag in flags:
        if flag["name"] != "speculative_algorithm" or flag["choices"]:
            continue
        names = set()
        try:
            from sglang.srt.speculative.spec_info import SpeculativeAlgorithm

            names |= {m.name for m in SpeculativeAlgorithm if m.name != "NONE"}
        except Exception as exc:
            notes.append(f"speculative algorithms not read from the registry: {exc}")
        builtins = re.search(r"Builtins?:\s*([A-Z0-9_,\s]+)", flag["help"] or "")
        if builtins:
            names |= {n.strip() for n in builtins.group(1).split(",") if n.strip()}
        if names:
            flag["choices"] = sorted(names)
            flag["choices_open"] = True


def sglang_parser(notes):
    from sglang.srt.server_args import ServerArgs

    parser = argparse.ArgumentParser(prog="sglang serve")
    ServerArgs.add_cli_args(parser)
    return parser


# -- vllm --------------------------------------------------------------------


def _vllm_shape(name, value):
    """(type, default, choices) of one environment_variables entry's getter."""
    kind, default, choices = "str", None, []
    for node in ast.walk(value):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = getattr(fn, "id", None) or getattr(fn, "attr", "")
        first = literal(node.args[0]) if node.args else None
        if fname in ("getenv", "get") and first == name and len(node.args) > 1:
            default = literal(node.args[1])
        elif fname.startswith("env_") and first == name:  # env_with_choices & co.
            if len(node.args) > 1:
                default = literal(node.args[1])
            if len(node.args) > 2 and isinstance(literal(node.args[2]), list):
                choices = literal(node.args[2])
            kind = "list" if "list" in fname or "set" in fname else "str"
    text = ast.unparse(value) if hasattr(ast, "unparse") else ""
    if re.search(r"\bbool\(", text) or re.search(r"in \(['\"]1['\"]", text):
        kind = "bool"
    elif re.search(r"\bint\(", text):
        kind = "int"
    elif re.search(r"\bfloat\(", text):
        kind = "float"
    return kind, default, choices


def typed(value, kind):
    """A default as the type the getter returns ("0" from a bool getter is False)."""
    if not isinstance(value, str):
        return value
    try:
        if kind == "bool":
            return value.strip().lower() in ("1", "true", "yes", "y")
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
    except ValueError:
        pass
    return value


def vllm_env(root, notes):
    path = os.path.join(root, "envs.py")
    source = open(path, encoding="utf-8").read()
    lines = source.splitlines()
    tree = ast.parse(source)
    table = None
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id == "environment_variables":
            table = node.value
    if not isinstance(table, ast.Dict):
        raise RuntimeError("environment_variables not found in vllm/envs.py")
    entries = []
    # No strict=: this runs on whatever Python the image ships.
    for key, value in zip(table.keys, table.values):  # noqa: B905
        name = literal(key)
        if not isinstance(name, str):
            continue
        kind, default, choices = _vllm_shape(name, value)
        default = typed(default, kind)
        help_text, section = comment_above(lines, key.lineno)
        entries.append({
            "kind": "env", "name": name, "type": kind, "default": default,
            "choices": choices, "help": help_text, "section": section,
            "where": f"envs.py:{key.lineno}",
        })
    notes.append(f"{len(entries)} env vars from vllm.envs")
    return entries


def vllm_parser(notes):
    # vllm writes help text only when the command line asks for help, and
    # decides on import: ask, before anything of vllm is imported.
    if not any("--help" in a for a in sys.argv):
        sys.argv.append("--help")
    # vllm's parser asks the platform for its defaults, and with no card
    # visible (this probe never takes one) nothing is detected: read them as
    # the CUDA platform would, which is what campaigns run on.
    import vllm.platforms as platforms

    if not getattr(platforms.current_platform, "device_type", ""):
        try:
            from vllm.platforms.cuda import CudaPlatform

            platforms.current_platform = CudaPlatform()
            notes.append("no GPU in the probe: flags read with the CUDA platform's defaults")
        except Exception:  # no CUDA driver library in the container either
            from vllm.platforms.cpu import CpuPlatform

            platforms.current_platform = CpuPlatform()
            notes.append("no CUDA driver in the probe: flags read with the CPU platform's "
                         "defaults, a few of which differ on GPUs")
    try:
        from vllm.utils.argparse_utils import FlexibleArgumentParser
    except ImportError:
        from vllm.utils import FlexibleArgumentParser
    make = None
    for module in ("vllm.entrypoints.common.cli_args", "vllm.entrypoints.openai.cli_args"):
        try:
            make = __import__(module, fromlist=["make_arg_parser"]).make_arg_parser
            break
        except (ImportError, AttributeError):
            continue
    parser = FlexibleArgumentParser(prog="vllm serve")
    if make is not None:
        return make(parser)
    from vllm.engine.arg_utils import AsyncEngineArgs

    return AsyncEngineArgs.add_cli_args(parser)


# -- flags -------------------------------------------------------------------


def jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def flags_of(parser):
    out = []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction) or not action.option_strings:
            continue
        long = [s for s in action.option_strings if s.startswith("--")]
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)) or (
            type(action).__name__ == "BooleanOptionalAction"
        ):
            kind = "bool"
        elif action.type is not None:
            kind = getattr(action.type, "__name__", str(action.type))
        else:
            kind = "str"
        default = action.default
        if default is argparse.SUPPRESS:
            default = None
        help_text = action.help if action.help is not argparse.SUPPRESS else ""
        if help_text and "%(" in help_text:
            try:
                help_text = help_text % {**vars(action), "prog": parser.prog, "default": default}
            except Exception:
                pass
        primary = long[0] if long else action.option_strings[0]
        out.append({
            "kind": "flag", "name": action.dest, "flag": primary,
            "aliases": [s for s in action.option_strings if s != primary],
            "type": kind, "default": jsonable(default),
            "choices": [jsonable(c) for c in action.choices] if action.choices else [],
            "negatable": type(action).__name__ == "BooleanOptionalAction",
            "help": " ".join((help_text or "").split()),
        })
    return out


def version_of(dist):
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:
        return ""


def main(engine):
    notes, entries, status = [], [], "ok"
    root = package_dir(engine)
    try:
        entries += (sglang_env if engine == "sglang" else vllm_env)(root, notes)
    except Exception as exc:
        status = "partial"
        notes.append(f"env vars unreadable: {type(exc).__name__}: {exc}")
    try:
        parser = (sglang_parser if engine == "sglang" else vllm_parser)(notes)
        flags = flags_of(parser)
        if engine == "sglang":
            sglang_open_choices(flags, notes)
        entries += flags
        notes.append(f"{len(flags)} flags from the serve parser")
    except Exception as exc:
        status = "partial"
        notes.append(f"flags unreadable: {type(exc).__name__}: {str(exc)[:300]}")
    if not entries:
        status = "failed"
    payload = {
        "engine": engine, "engine_version": version_of(engine), "status": status,
        "notes": notes, "entries": entries,
        "python": sys.version.split()[0],
    }
    blob = base64.b64encode(zlib.compress(json.dumps(payload).encode(), 9)).decode()
    sys.stdout.write(f"\n{MARKER}{blob}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sglang")
