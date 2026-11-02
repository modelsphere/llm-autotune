# Catalog variants (test fixtures)

Every `kind: Variant` file of [model-catalog](https://github.com/modelsphere/model-catalog)
at `cb720f5f18a6ea28b992329691145df3fbbd50a5`, copied as `<model>__<id>.yaml`. Apache-2.0, like that repository.

`tests/test_recipe.py` reads each one as a recipe and checks that the flag/address
conversion is lossless on it. Re-copy when the catalog grows a new shape.

`deepseek-v4.1-flash-1017-w8a8__vllm-dp2-tp8-910b4-2node.yaml` was added later, from
`67d4689`: an Ascend variant that requests its cards under `resources` with
`requires.gpus: 0`, on two nodes, with a multi-line `commandOverride`.
