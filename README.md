# CryoWeave

**Weaving observations into evidence.**

**把多源观测，织成科研证据。**

面向寒区湖泊、冻土、交通走廊与多源遥感研究的 DeepSeek Harness 插件集。CryoWeave 将 GEE、InSAR、UAV、工程监测、物理模型和论文证据链组织成可独立安装、可复现验证的科研工具。

[![CI](https://github.com/ali820/CryoWeave/actions/workflows/ci.yml/badge.svg)](https://github.com/ali820/CryoWeave/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 为什么做 CryoWeave？

科研计算不应该依赖“让 AI 凭感觉回答”，而应该由可验证的程序完成。CryoWeave 让 Agent 负责理解项目上下文、组织流程和解释异常，让确定性工具负责计算、检查与留痕。

- **证据优先**：连接结论、数字、源数据、代码、图件和权威版本；
- **真正执行**：插件返回结构化分析或 QA 结果，而不只是生成提示词；
- **独立插件**：每个插件可单独安装、升级和验证，不要求一次启用全部能力；
- **默认安全**：只读优先、限制项目根目录、拒绝路径逃逸、不读取凭据；
- **科学边界清晰**：区分观测、模型、弱标签、合成演示和生成内容，不把原型包装成正式结论。

## 当前可用：Research Evidence

`research-evidence` 是首个完成真实项目验证的插件，提供 `research_evidence_audit` 工具：

| 能力 | 当前实现 |
| --- | --- |
| 权威入口 | 查找 `CURRENT_AUTHORITY.md`，提取标题、更新日期、大小、修改时间和 SHA-256 |
| 图件完整性 | 验证 `*.export.json` 中源数据、缓存和输出文件的路径、字节数和 SHA-256 |
| 主张—证据表 | 检查 `claim_evidence_map*.csv` 的必填字段、重复 ID、来源和门控状态 |
| 数字冲突 | 汇总 `numeric_conflict_register.csv` 的已解决和未解决记录 |

真实只读验证包括：

- **20-qxp**：扫描 2,817 个文件，解析 50 份图件清单，验证 178 个引用，并发现历史绝对路径、清单结构和字节数问题；
- **15 双论文工作区**：检查 27 条主张及 3 份数字冲突表，识别门控/状态阻断，确认 12 条登记冲突均已解决。

详见[插件说明](plugins/research-evidence/README.md)和[真实验证记录](plugins/research-evidence/VALIDATION.md)。

## 新增：Research Run Ledger

`research-run` 为每次研究分析建立独立运行账本，支持 `create`、`list`、`get`、`update_status`、`register_artifacts` 和 `verify`：

- 创建 pipeline 运行及步骤，记录输入配置和 SHA-256；
- 更新 planned/running/complete/failed/cancelled 状态并保留历史；
- 登记输出产物，之后检测缺失或内容变化；
- 状态只写入独立 `stateRoot`，不修改原始研究项目；
- 敏感配置键自动脱敏，首版不开放任意命令执行。

详见[运行账本说明](plugins/research-run/README.md)和[生命周期验证](plugins/research-run/VALIDATION.md)。

## 新增：Corridor Analysis Units

`corridor-analysis` 将路线转换为标准化的里程—缓冲研究单元：

- 检查本地 GeoJSON/Shapefile/GeoPackage 或 GEE FeatureCollection；
- 按公里分段，可指定方向、起止里程和工作 CRS；
- 生成独立缓冲环，避免累计缓冲重复计数；
- 输出 GeoJSON、CSV、manifest 和几何/文件哈希；
- 按环、按段或联合分组统计，支持权重和像元覆盖汇总。

真实独库验证已生成 **12 段 × 5 环 = 60 个研究单元**，路线合并长度为 **525.730278 km**。详见[走廊分析说明](plugins/corridor-analysis/README.md)和[真实路线验证](plugins/corridor-analysis/VALIDATION.md)。

## 迁移完成：GEE Analysis 1.1

现有 GEE 插件已迁移为 `@cryo-research/gee`，保留 `gee_status` 与 `gee_analyze`，并新增 `gee_corridor_analyze`：

- 直接读取走廊 `units.geojson`，逐单元计算 Sentinel-2 NDVI 和有效像元数；
- 支持 `unitIds`、`ringIds`、影像数与单元数上限；
- 输出 NDVI CSV 和 manifest，可继续由走廊统计和 `research-run` 复用；
- 已验证前 3 个单元的真实在线计算及完整跨插件链路。

详见[GEE 使用说明](plugins/gee-analysis/README.md)和[联合验证记录](plugins/gee-analysis/VALIDATION.md)。

## 新增：Environmental Indices and TVDI

`environmental-indices` 支持本地栅格的 NDVI、FVC、LST、NDMI、NDSI 和 TVDI 计算：

- 自动对齐不同原点、行列方向与 CRS 的栅格；
- 支持缩放、范围过滤、NoData 和 Landsat QA_PIXEL；
- 输出指标栅格、有效像元统计、TVDI 干湿边拟合和 manifest；
- 真实独库 NDVI/LST 样例已验证 12,141 个有效像元及 8,051 个 TVDI 拟合点。

详见[环境指标说明](plugins/environmental-indices/README.md)和[真实栅格验证](plugins/environmental-indices/VALIDATION.md)。

## 快速开始

### 环境

- DeepSeek Harness 当前兼容版本；
- Node.js 24、pnpm、Python 3.12；
- `research-evidence` 和 `research-run` 只需 Python 标准库；`corridor-analysis`、`gee-analysis` 与 `environmental-indices` 的依赖见各自 `requirements.txt`。

### 克隆与验证

```bash
git clone https://github.com/ali820/CryoWeave.git
cd CryoWeave
pnpm install
python scripts/check_repo.py
node --check plugins/research-evidence/index.js
python plugins/research-evidence/evidence_worker.py --self-test
```

### 在 DSH 中安装

在 DSH 的“插件”页面选择添加第三方插件，输入本目录的绝对路径：

```text
<CryoWeave>/plugins/research-evidence
```

也可使用官方插件管理命令：

```bash
dsh plugin --profile <profile> add <absolute-path-to-CryoWeave/plugins/research-evidence>
```

安装与激活是不同状态；按 DSH 提示启用或重启。

### 对 Agent 说

```text
用 research_evidence_audit 审计 <项目绝对路径>，
检查权威文件和图件清单，按问题类型汇总，不要修改任何文件。
```

也可以只检查主张与数字冲突：

```text
审计 <项目绝对路径>，只执行 claims 和 conflicts，
列出锁定主张和未解决数字冲突。
```

## 插件路线图

CryoWeave 采用 Monorepo，但不会为规划中的功能建立空壳。

| 方向 | 插件 |
| --- | --- |
| 证据与运行 | `research-evidence`（MVP）、`research-run` |
| 遥感与走廊 | `gee-analysis`、`environmental-indices`、`corridor-analysis`、`insar-ops` |
| 湖泊与事件 | `lake-mapper`、`lake-lineage`、`hydro-event` |
| 现场与模型 | `field-multisource`、`model-experiments`、`physics-scenarios` |

现有独立 GEE 插件将在保持可用安装的前提下迁移。完整状态和验收门槛见 [ROADMAP.md](ROADMAP.md)。

## 仓库结构

```text
CryoWeave/
├─ plugins/            # 可独立安装的 DSH Bundle
├─ shared/schemas/     # run、claim、finding 共享结构
├─ docs/               # 架构、证据模型和开发规范
├─ scripts/            # 仓库级检查
├─ examples/           # 可公开的小型示例
└─ .github/workflows/  # 持续集成
```

## 开发与数据边界

- 每个插件必须有离线自测和至少一次真实只读验证；
- 不提交凭据、大型遥感数据、模型权重、未公开监测数据或审稿材料；
- 权威规则由研究者确认，插件暴露冲突，不替代科学判断；
- 一致性检查不等于独立复算，`resolved` 也不自动等于科学真实。

开发规范见[插件开发指南](docs/plugin-development.md)、[架构说明](docs/architecture.md)和[证据数据模型](docs/evidence-model.md)。

## License

[MIT License](LICENSE) © 2026 ali820
