# Cryo Research Plugins

面向寒区湖泊、冻土、交通走廊与多源遥感研究的 DeepSeek Harness 插件仓库。

本仓库采用 Monorepo：**统一管理和验证，插件独立安装、独立升级。**当前只实现具有明确验收标准的能力，不为规划中的插件建立空壳。

## 当前状态

- `research-evidence`：MVP 已完成，支持权威文件发现、图件导出清单校验、主张—证据表检查和数字冲突汇总；见[使用说明](plugins/research-evidence/README.md)与[验证记录](plugins/research-evidence/VALIDATION.md)。
- `gee-analysis`：已有可用版本，暂保留原安装位置，后续迁移，避免影响现有使用。
- 其他核心插件见 [ROADMAP.md](ROADMAP.md)，尚未实现。

## 原则

1. 计算保持确定性，Agent 负责上下文、流程组织与异常解释。
2. 每个插件必须产生可检查的实际结果，不能只包装聊天提示词。
3. 默认只读、显式项目根目录、限制文件范围，不读取凭据。
4. 权威规则由研究者确认；插件暴露冲突，不代替科学判断。
5. 观测、模型、弱标签、合成演示和生成内容分层管理。

## 仓库检查

```powershell
python scripts/check_repo.py
node --check plugins/research-evidence/index.js
python plugins/research-evidence/evidence_worker.py --self-test
```

## 文档

- [架构说明](docs/architecture.md)
- [插件开发规范](docs/plugin-development.md)
- [证据数据模型](docs/evidence-model.md)
- [插件路线图](ROADMAP.md)

当前为私有研发仓库，尚未选择开源许可证，见 [LICENSE.md](LICENSE.md)。
