# BFCL V4 工具调用调优

AutoTune 支持 LLMBench 的 `bfcl_v4` 模块，可以作为独立负载，也可以在 sweep/replay 后附加质量检测。运行和官方评分由 LLMBench 完成，AutoTune 不需要安装 BFCL Python 环境。LLMBench 服务必须部署包含该模块及其运行依赖的版本，推理端点需要支持原生流式工具调用，并配置匹配模型的 tool-call parser。

默认安装脚本固定的 LLMBench v0.3.0 尚未包含 BFCL。使用 BFCL 时，API 和 worker 均需部署包含该模块的 LLMBench 镜像。

在新建调优任务的负载选择中选 **BFCL V4**。选择固定题集、每类题数、题目并发、超时、重试和遥测覆盖率要求。独立 BFCL 默认按 `bfcl_v4.total_tpm`（输入加输出的总 TPM）最大化排名，生成的 benchmark 会自动将 `total_tpm` 加入必报指标；缺失 token usage 时 LLMBench 会指出缺失的指标。也提供 BFCL accuracy 内置目标；生产调优应为准确率配置经过校准的红线。

如需保留原来的性能负载，在质量检测中选择 **BFCL V4**。默认只测生产配置和入围候选，也可选择每轮测量。质量底线使用 `bfcl_v4/accuracy[overall]`，例如允许比生产低 5 个百分点时，保存的红线为：

```json
{"metric": "bfcl_v4/accuracy[overall]", "op": ">=", "relative_to": "production", "value": -0.05}
```

生产均分仅使用相同 BFCL 题集、每类题数及生成设置的测量。对应生产分数尚不存在时，候选不会通过相对底线；生产结果到达后会重新评分。API 支持同时追加 OpenCompass `quality` 和 `bfcl_quality`，界面选择其中一个。独立 BFCL 负载已经测量工具调用质量，不能再追加一个 `bfcl_quality`。

## API 配置

`POST /api/campaigns` 的负载字段可写为：

```json
{
  "benchmark_spec": {
    "kind": "bfcl",
    "bfcl": {
      "task_set": "bfcl-v4-tool-use-v1",
      "cases_per_category": 20,
      "concurrency": 2,
      "request_timeout_seconds": 120,
      "max_seconds": 1800,
      "request_retries": 0,
      "probe_endpoint": false,
      "required_metrics": ["accuracy", "passed_cases_per_hour", "ttft_p90_ms"],
      "min_ttft_coverage": 1,
      "min_usage_coverage": 0,
      "min_cache_usage_coverage": 0
    }
  },
  "objective": {
    "target_metric": "bfcl_v4.total_tpm",
    "direction": "maximize",
    "redlines": [{"metric": "bfcl_v4/accuracy[overall]", "op": ">=", "value": 0.8}]
  }
}
```

`0.8` 只是准确率底线示例，应根据生产端点校准。附加质量检测时，将相同 BFCL 参数放在 `benchmark_spec.bfcl_quality` 下，主负载仍使用 `kind: sweep` 或 `kind: replay`。验证阶段对应 `verify_benchmark_spec`，独立 BFCL 验证未指定排名指标时也默认按总 TPM 排名。

已有 benchmark 仍可以直接填写 slug。管理员可通过 `POST /api/benchmarks/ensure` 创建和锁定模板 `autotune-bfcl-v4-tool-use-v1` 或 `autotune-bfcl-v4-multi-turn-long-context-v1`。自动生成的 benchmark 使用包含 BFCL 参数的哈希 slug；原有未包含 BFCL 的配置保持原来的 slug。

手动验证 `POST /api/campaigns/{id}/validate` 支持：

```json
{"configs": [{"tp": 2}], "machine": "gpu-node", "quality_metrics": ["bfcl_v4/accuracy[overall]"], "quality_max_drop": 5}
```

此时验证 benchmark 必须包含 BFCL。原来的 `quality_tasks` OpenCompass 参数继续兼容。

## 指标及结果解释

| 指标 | 含义 |
| --- | --- |
| `bfcl_v4/accuracy[overall]` | 官方评分通过的题目比例，0–1 |
| `bfcl_v4/accuracy[multi_turn_long_context]` | 长上下文多轮类别的准确率；其他类别用相同写法 |
| `bfcl_v4/passed_cases_per_hour` | 通过题数除以生成耗时，不包含评分耗时 |
| `bfcl_v4/ttft_ms@p90` | 首个正文或工具调用 token 的延迟，不包含 reasoning token |
| `bfcl_v4/e2e_latency_ms@p90` | 单次 HTTP 请求端到端延迟 |
| `bfcl_v4/total_tpm_per_gpu` | 输入加输出 TPM，除以实际 GPU 数 |
| `bfcl_v4/ttft_coverage` | 成功请求中具有 TTFT 样本的比例 |
| `bfcl_v4/input_usage_coverage`、`output_usage_coverage`、`cache_usage_coverage` | 对应 usage 遥测的覆盖率 |

原始指标如 `bfcl_v4.accuracy` 仍可访问。缺失遥测不会被填成零，也不会推导模块未报告的 ITL 或 TTFT mean。题目并发不等于 HTTP 并发：多轮题内请求按顺序执行，重试还会增加 HTTP attempt 数；报告分别保留 `case_concurrency` 和 `active_http_peak`。

报告支持 `table` 的 `type: bfcl` 和 `chart` 的 `type: bfcl`（附 `scenario: bfcl_v4`），展示准确率、每小时通过题数和遥测。质量表只包含 BFCL 的准确率，吞吐和 coverage 不会被误显示为质量分数。

题集包括两题 smoke、五类 tool-use 和独立 multi-turn-long-context。Smoke 只能每类一题；其余每类支持 1–200 题。默认自动生成配置使用每类 20 题；不要用极小样本判断微小精度变化。比较时固定题集、样本数、并发、超时和重试策略，报告会拒绝协议参数不同的直接比较。LLMBench artifact 中的 manifest 还记录 BFCL revision 和 `protocol_hash`，跨 LLMBench 升级比较时应核对这些信息。

## 单个 run 内的多并发扫描

独立 BFCL 负载可增加 `concurrency_grid`，例如：

```json
{"kind":"bfcl","bfcl":{"task_set":"bfcl-v4-multi-turn-long-context-v1","concurrency_grid":[1,3,5,6,8],"max_seconds":1800,"max_total_seconds":9000}}
```

单次部署、单个 run 和一个 LLMBench 模块按升序执行全部点。固定并发字段在扫描时不参与执行。
每点单独生成题目结果、官方评分和遥测，保存为 `c<N>`；报告展示完整 TPM/准确率曲线及点表。
顶层指标来自满足 benchmark redline 的最高总 TPM 有效点。部分点失败、超时或总预算耗尽时，
只要仍有有效且达标的点就可以入榜；没有达标点则不能入榜。用户主动取消时，整次提交
不能入榜，即使已有达标点。报告保留有效/计划点数、各点失败原因，并标记部分完成或取消。
`max_seconds` 是每点预算，总预算默认是点数乘每点预算，也可用 `max_total_seconds` 指定。
同一次扫描共用端点缓存，不清缓存，不增加合成 warmup；比较时保持扫描顺序和协议一致。
不同 grid 的报告默认判为不可直接比较。省略 grid 保留原来的固定并发行为。

需要部署支持扫描的 LLMBench 版本。提交级扫描参数为
`extra_params.bfcl_concurrency_grid`，不能与单值 concurrency override 同时使用；
LLMBench 冻结实际生效的 grid，AutoTune 保留全部点及实际选中的题目并发。
