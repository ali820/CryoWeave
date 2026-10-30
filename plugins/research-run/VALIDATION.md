# Research Run 验证记录

验证日期：2026-10-30。

## 离线自测

`python plugins/research-run/run_worker.py --self-test` 返回：

```json
{"ok": true, "selfTest": "passed", "checks": 10}
```

覆盖：运行创建、敏感配置脱敏、运行列表、状态流转、输出登记、当前文件验证、篡改后的过期检测、非法状态回退、相对路径逃逸和 manifest 读取。

## 真实 Host 生命周期验证

通过 `plugins/research-run/tests/host_smoke.mjs` 调用实际 `research_run` 工具，使用只读项目：

`E:/文章同步/16-dk2/5-独库植被过渡带`

输入文件：`config/r1_median.yml`。所有新增状态与结果均写入独立 `stateRoot`，未修改项目文件。

验证流程：

1. `create` 创建 `validation-gee-ndvi` 运行并哈希输入；
2. `update_status` 将运行和分析步骤设为 `running`；
3. `register_artifacts` 登记 `state://runs/<runId>/result.json` 并设为 `complete`；
4. `verify` 返回 `current`；
5. 人为修改测试产物后，`verify` 返回 `stale`，并给出具体哈希/字节变化；
6. `list` 能按 pipeline 检索运行；
7. `apiKey` 测试值在 manifest 中保存为 `[REDACTED]`，未保留原值。

最终返回：

```json
{
  "ok": true,
  "lifecycle": "create>running>complete",
  "current": "current",
  "stale": "stale",
  "redacted": true
}
```

## 当前边界

- 首版不直接执行项目脚本，只登记 Agent 或其他工具已经执行的工作；
- 没有开放任意命令执行；
- 原始研究项目只读，新增写入仅限插件配置的 `stateRoot`；
- 安装与 Host 激活是不同状态，是否出现在当前会话取决于 DSH 重启/加载。
