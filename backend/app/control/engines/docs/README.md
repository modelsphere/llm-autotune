# Engine docs shipped with the platform

The engines' own reference pages, copied verbatim, so the engine vocabulary
(`vocabulary_store.py`) can describe environment variables an image's code
does not describe, without reaching GitHub from the cluster.

One directory per engine and version: `<engine>/<version>/<page>`. An image
reads the page for its exact version; an image of a version not shipped here
tries `ENGINE_DOCS_URL_<ENGINE>` first and then uses the nearest shipped
version, with a note saying which.

| Page | Source | License |
| --- | --- | --- |
| `sglang/0.5.19/environment_variables.mdx` | [sgl-project/sglang](https://github.com/sgl-project/sglang) `v0.5.19`, `docs/docs/references/environment_variables.mdx` | Apache-2.0, © the SGLang authors |

To add a version: copy the page from that tag of the engine's repository into
a new `<version>/` directory and add a row above.
