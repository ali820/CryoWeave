# Research Run Ledger

科研运行与资产账本插件。让 Agent 为一次分析创建可回查的运行记录，登记输入、输出、状态与 SHA-256，并检查结果是否缺失或已过期。

## `research_run`

支持六个动作：

| 动作 | 用途 |
| --- | --- |
| `create` | 创建运行，指定 `projectRoot`、`pipeline`，可附 `config`、`inputs`、`steps` |
| `list` | 按管线、状态或项目筛选运行 |
| `get` | 读取完整 run manifest |
| `update_status` | 更新运行/步骤状态并追加历史，可用于开始、完成、失败或失败后续跑 |
| `register_artifacts` | 登记输出文件及 SHA-256，可同时完成步骤或运行 |
| `verify` | 重新计算输入输出哈希，发现缺失、字节变化和内容变化 |

## 一次典型流程

```text
创建运行
→ update_status: running
→ 用其他插件或工具执行分析
→ register_artifacts 登记结果并设为 complete
→ 之后用 verify 检查源文件和结果是否变化
```

示例：为独库 NDVI 分析创建运行：

```json
{
  "action": "create",
  "projectRoot": "E:/文章同步/16-dk2/5-独库植被过渡带",
  "pipeline": "gee-ndvi-timeseries",
  "config": {
    "asset": "projects/ee-my-road/assets/dukuregion",
    "startDate": "2024-06-01",
    "endDate": "2024-10-01",
    "scale": 30
  },
  "steps": ["inventory", "analysis", "figure"]
}
```

开始分析：

```json
{
  "action": "update_status",
  "runId": "<create 返回的 runId>",
  "status": "running",
  "step": "analysis",
  "note": "Start GEE NDVI time series"
}
```

登记结果：

```json
{
  "action": "register_artifacts",
  "runId": "<runId>",
  "artifacts": ["outputs/ndvi_2024.csv"],
  "status": "complete",
  "step": "analysis"
}
```

之后验证：

```json
{
  "action": "verify",
  "runId": "<runId>"
}
```

`auditStatus: stale` 表示输入或输出已经缺失/改变；`current` 表示登记的文件仍然一致。

## 状态与恢复

- `planned → running / cancelled`
- `running → complete / failed / cancelled`
- `failed → running / cancelled`
- `cancelled → running`
- `complete` 不允许直接回退；需要新运行时应新建 run，而不是覆盖历史。

## 存储与安全

- run manifest 只写入插件配置的 `stateRoot`，不会写入原始项目；
- 输入必须位于 `projectRoot` 内；
- 输出可位于 `projectRoot`，或使用 `state://` 指向 `stateRoot` 内的文件，例如 `state://runs/<runId>/result.json`；
- 拒绝路径逃逸、绝对路径越权和符号链接逃逸；
- `token`、`secret`、`password`、`credential`、`apiKey` 等配置键自动脱敏；
- 单文件默认哈希上限 256 MiB，路径清单最多 200 项。

## 当前不做什么

首版**不直接执行项目脚本**。Agent 可以调用 GEE、证据审计或其他工具，再用本插件登记执行过程。后续可加入经过管理员显式配置的 pipeline runner，但不会开放任意命令执行。
