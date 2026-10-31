# CryoWeave 核心插件路线图

状态：`planned` 未开始；`in-progress` 正在开发；`mvp` 已有最小可用版本；`stable` 已有完整验证。

| 顺序 | 插件 | 主要产出 | 状态 |
| --- | --- | --- | --- |
| 1 | `research-evidence` | 权威状态、证据门、数字/图件审计报告 | mvp |
| 2 | `research-run` | 项目配置、运行清单、断点续跑和资产登记 | mvp |
| 3 | `corridor-analysis` | 走廊单元、覆盖账本、距离梯度和对照 QA | mvp |
| 4 | `gee-analysis` | GEE 观测锚点、统计和导出任务 | mvp |
| 5 | `environmental-indices` | NDVI/FVC/LST/TVDI 等指标和趋势 | mvp |
| 6 | `lake-mapper` | 水体、岸线、面积、重心、SI/FD | mvp |
| 7 | `lake-lineage` | 湖泊谱系、队列、事件和交通共位 | mvp |
| 8 | `hydro-event` | 面积—水位—库容、闭合账和事件重建 | mvp |
| 9 | `insar-ops` | InSAR 任务、参考版本、覆盖和结果 QA | mvp |
| 10 | `field-multisource` | 地温/UAV/GPR/RTK 时空整合 | mvp |
| 11 | `model-experiments` | 分组验证、滚动回测、SHAP 和敏感性 | planned |
| 12 | `physics-scenarios` | 物理模型场景、守恒和集合不确定性 | planned |

## 验收门槛

每个插件至少满足：

- 有一个清晰的科研任务和真实项目样例；
- 有结构化输入输出和范围限制；
- 有离线自测及至少一次真实只读验证；
- 有中文/英文元数据、有效图标和使用说明；
- 明确不做什么，不把原型或待验证结果写成正式结论。
