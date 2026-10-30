# 证据数据模型

## Finding

统一输出：`code`、`severity`、`path`、`message`，可附 `expected`、`actual` 和 `status`。对应结构见 `shared/schemas/finding.schema.json`、`run-manifest.schema.json` 和 `claim-evidence.schema.json`。

严重度：

- `error`：缺失、哈希不符、无法解析或明确禁用状态；
- `warning`：待办、过期风险、未决冲突或不完整证据；
- `info`：正常状态和统计摘要。

## 当前支持的结构化证据

1. `CURRENT_AUTHORITY.md`：项目当前权威入口；
2. `*.export.json`：图件源数据、缓存和输出的路径/SHA-256；
3. `claim_evidence_map*.csv`：主张、证据状态、门控和允许/禁止措辞；
4. `numeric_conflict_register.csv`：数字冲突及解决状态。

首版不猜测论文自然语言中的数字，也不判断哪个科学结论正确。它验证结构化证据的完整性并暴露需要人工处理的项目。
