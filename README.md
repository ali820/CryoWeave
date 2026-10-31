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

## 新增：Lake Mapping and Morphology

`lake-mapper` 将水体提取与湖泊形态分析连接起来：

- 从掩膜、MNDWI 或 NDWI 提取水体并过滤小图斑；
- 直接分析已有湖泊矢量，计算面积、周长、重心、长短轴、紧致度、SI 和近似 FD；
- 输出 WGS84 多边形、形态表和 manifest；
- 真实四湖样例已验证，且明确保留源数据中的缓冲语义。

详见[湖泊分析说明](plugins/lake-mapper/README.md)和[真实四湖验证](plugins/lake-mapper/VALIDATION.md)。

## 新增：Lake Lineage and Exposure

`lake-lineage` 按双向主导重叠追踪年度湖泊，明确区分续接、合并、分裂、消失、歧义和小事端：

- 输出谱系、事件台账、稳定端点队列和端点几何；
- 支持重叠阈值、完整性过滤、端点面积变化和队列资格控制；
- 计算端点湖泊与现状交通网络的最近距离及阈值共位；
- 真实 Lake_TP 2020–2023 数据已验证 21,823 条谱系和 34,916 条事件记录。

交通结果仅表示现状平面共位，不自动升级为淹没、损伤或风险预测。详见[谱系与暴露说明](plugins/lake-lineage/README.md)和[真实年度验证](plugins/lake-lineage/VALIDATION.md)。

## 新增：Hydro Event and Water Balance
`hydro-event` 将库容计算与水量闭合连接起来：

- 从 DEM 生成高程—面积—库容曲线并插值储量；
- 计算释放量与平均出流，支持显式堰流情景；
- 用 `outcomeTerm` 和 `nonSourceTerms` 明确结果项与来源项的符号约定；
- 真实卓乃湖库容验证得到 3.139522 km³ 释放量，闭合率 87.178653%。

详见[水文事件说明](plugins/hydro-event/README.md)和[真实库容验证](plugins/hydro-event/VALIDATION.md)。

## 新增：InSAR Operations and QA

`insar-ops` 负责 Sentinel-1 档案与结果 QA，而不替代 ISCE/MintPy 反演：

- 盘点轨道、日期和重复档案，按时间基线生成干涉对计划；
- 对速率/相干性产品做异常簇分析，输出面积、质心、速度统计和有效像元；
- 核验参考版本与文件 SHA-256，避免旧参考产品混用；
- 真实镇原产品已验证 263 个异常簇，异常面积 38.246 km²。

异常簇仅用于 QA 与现场复核，不自动分类为滑坡或工程损伤。详见[InSAR 使用说明](plugins/insar-ops/README.md)和[真实产品验证](plugins/insar-ops/VALIDATION.md)。

## 新增：Field Multisource Observations

`field-multisource` 处理现场观测表格，支持：

- CSV/Excel 源检查、时间范围、缺失、越界、重复和时间间隔 QA；
- 按时间最近邻，或结合经纬度进行时空配对；
- 输出配对行索引、时间差、距离和配对值；
- 计算 MAE、RMSE、偏差、相关系数和 R²。

真实上下游地温验证完成 1,373 行 QA 和 1,273 条同日期配对，RMSE 为 2.164967°C；该结果表示跨站点差异，不是传感器精度证明。详见[多源观测说明](plugins/field-multisource/README.md)和[真实现场验证](plugins/field-multisource/VALIDATION.md)。

## 新增：Model Experiments and Validation

`model-experiments` 对科研表格执行：

- 分组交叉验证与滚动回测；
- 同折基线比较；
- 置换重要性与标准差；
- 跨折重复特征、分组重叠和目标列泄漏检查。

真实分类表 5 折空间分组验证 Accuracy 为 0.968901、ROC AUC 为 0.989415；置换重要性不是 SHAP，也不证明因果。详见[模型实验说明](plugins/model-experiments/README.md)和[真实模型验证](plugins/model-experiments/VALIDATION.md)。

## 新增：Physics Scenarios and Ensembles

`physics-scenarios` 提供可解释的解析物理场景：

- 周期表面温度下的一维热传导；
- Terzaghi 一维固结与沉降守恒检查；
- 给定范围的参数集合和分位数；
- 全部输出明确 `scenarioOnly`，不冒充全耦合求解或预报。

真实热学参数验证得到热扩散率 0.075 m²/day、阻尼深度 0.15451 m，100 个集合样本的温度分位数也已复核。详见[物理场景说明](plugins/physics-scenarios/README.md)和[真实物理验证](plugins/physics-scenarios/VALIDATION.md)。

## 快速开始

### 环境

- DeepSeek Harness 当前兼容版本；
- Node.js 24、pnpm、Python 3.12；
- `research-evidence` 和 `research-run` 只需 Python 标准库；`corridor-analysis`、`gee-analysis`、`environmental-indices`、`lake-mapper`、`lake-lineage`、`hydro-event`、`insar-ops`、`field-multisource`、`model-experiments` 与 `physics-scenarios` 的依赖见各自 `requirements.txt`。

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
