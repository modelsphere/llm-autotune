"""A report as one self-contained HTML file.

Everything the page needs rides inside it: each language's markdown, the
frozen comparison, assets as data URIs, and the renderer bundle (markdown,
tables, charts) that the report page itself uses. Nothing is fetched when the
file is opened, so it can be dropped onto a docs or blog site as it is.

The renderer is built from the frontend (`npm run build:report`) into
`app/agent/static/report-renderer.js`; this module only reads it.
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from app.db.models import AgentReport

RENDERER = Path(__file__).resolve().parent / "static" / "report-renderer.js"


class RendererMissing(RuntimeError):
    pass


def filename(report: AgentReport) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (report.title or "report").lower()).strip("-")
    return f"{slug or 'report'}-{report.id}.html"


def _payload(r: AgentReport) -> dict[str, Any]:
    return {
        "id": r.id,
        "lang": r.lang or "en",
        "title": r.title,
        "markdown": r.markdown,
        "comparison": r.comparison or {},
        "labels": list(r.labels or []),
        "assets": {
            a["name"]: f"data:{a.get('content_type') or 'application/octet-stream'};base64,"
            f"{a.get('data_base64', '')}"
            for a in (r.assets or [])
            if isinstance(a, dict) and a.get("name")
        },
    }


def _script_json(value: Any) -> str:
    """JSON safe to sit inside a <script> element: no `</script>` can close it."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


def render_html(report: AgentReport, translations: list[AgentReport]) -> str:
    if not RENDERER.is_file():
        raise RendererMissing(
            f"{RENDERER} is missing — run `npm run build:report` in frontend/ and commit it"
        )
    bundle = RENDERER.read_text(encoding="utf-8").replace("</script", "<\\/script")
    data = {
        "initial": report.lang or "en",
        "reports": [_payload(r) for r in (translations or [report])],
    }
    title = html.escape(report.title or "Report")
    lang = "zh-CN" if (report.lang or "en") == "zh" else "en"
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
</head>
<body>
<div id="autotune-report"></div>
<script type="application/json" id="autotune-report-data">{_script_json(data)}</script>
<script>{bundle}</script>
<script>AutotuneReport.mountStandalone("autotune-report", "autotune-report-data");</script>
</body>
</html>
"""


# --- the source bundle -------------------------------------------------------

_README = """# {title}

The source of a performance report: the markdown the author wrote and the
data its charts, tables and commands are drawn from.

| File | What it is |
|---|---|
{files}| `report.json` | the manifest: per language, its title, files and config labels |
| `report.html` | the report, rendered — self-contained, opens offline |
| `report-renderer.js` | the renderer `report.html` uses, to render the markdown elsewhere |

## Blocks

Charts, tables and serving commands are not written out in the markdown. They
are fenced blocks naming what to draw, filled from the comparison file:

````markdown
```chart
type: summary
```
````

`chart` takes `type: summary | sweep | agentic` (the last two with a
`scenario:`), `table` takes `type: setup | summary | quality | scenarios | diff`
(`diff` with an `attempt:`), and `command` takes `config: baseline | <attempt>`.
A plain markdown viewer shows them as code blocks; the renderer draws them.

## Rendering it on another site

```html
<div id="report"></div>
<script src="report-renderer.js"></script>
<script>
  Promise.all([fetch('report.en.md').then(r => r.text()),
               fetch('comparison.json').then(r => r.json())])
    .then(([markdown, comparison]) => AutotuneReport.renderReport(
      document.getElementById('report'),
      {{ markdown, comparison, lang: 'en', labels: [], resolveAsset: (name) => name }}))
</script>
```

Take `lang`, `labels` and the file names from `report.json`. Images the
markdown references by bare name sit next to it in this folder.
"""


def bundle_name(report: AgentReport) -> str:
    return filename(report).removesuffix(".html")


def render_zip(report: AgentReport, translations: list[AgentReport]) -> bytes:
    """Everything needed to render the report anywhere, as a zip: each
    language's markdown, the frozen comparison, the assets, a manifest, a
    README, and — when the renderer is built — the renderer and the rendered
    self-contained page."""
    reports = translations or [report]
    comparisons = {json.dumps(r.comparison or {}, sort_keys=True) for r in reports}
    shared = len(comparisons) == 1
    root = bundle_name(report)
    buf = io.BytesIO()
    manifest: dict[str, Any] = {"initial": report.lang or "en", "reports": []}
    listed: list[tuple[str, str]] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name: str, data: str | bytes) -> None:
            z.writestr(f"{root}/{name}", data)

        if shared:
            put("comparison.json", json.dumps(reports[0].comparison or {}, indent=2,
                                              ensure_ascii=False))
            listed.append(("comparison.json", "the data every block is drawn from"))
        seen_assets: set[str] = set()
        for r in reports:
            lang = r.lang or "en"
            md, cmp_name = f"report.{lang}.md", "comparison.json"
            put(md, r.markdown or "")
            listed.append((md, f"the report in `{lang}`"))
            if not shared:
                cmp_name = f"comparison.{lang}.json"
                put(cmp_name, json.dumps(r.comparison or {}, indent=2, ensure_ascii=False))
                listed.append((cmp_name, f"the data the `{lang}` report's blocks are drawn from"))
            for a in r.assets or []:
                name = a.get("name") if isinstance(a, dict) else None
                if name and name not in seen_assets:
                    seen_assets.add(name)
                    put(name, base64.b64decode(a.get("data_base64") or ""))
                    listed.append((name, "an image the markdown references"))
            manifest["reports"].append({
                "id": r.id, "lang": lang, "title": r.title, "markdown": md,
                "comparison": cmp_name, "labels": list(r.labels or []),
            })
        put("report.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        if RENDERER.is_file():
            put("report.html", render_html(report, reports))
            put("report-renderer.js", RENDERER.read_bytes())
        files = "".join(f"| `{n}` | {d} |\n" for n, d in listed)
        put("README.md", _README.format(title=report.title or "Report", files=files))
    return buf.getvalue()
