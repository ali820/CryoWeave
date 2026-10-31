# Model Experiments 验证记录

验证日期：2026-10-31。

## 离线自测

`python plugins/model-experiments/model_worker.py --self-test` 返回：

```json
{"ok": true, "selfTest": "passed", "checks": 4}
```

覆盖：分组交叉验证、基线比较、置换重要性、滚动回测和路径逃逸。

## 真实分类表验证

数据：`classifier_training.csv`，3,923 个完整对象，湖泊/河流标签，5 个形态特征，`spatial_block` 分组。

模型：Random Forest，100 棵树，随机种子 42，5 折空间分组交叉验证。

### 模型与基线

- Accuracy：**0.968901**；
- Precision：**0.974480**；
- Recall：**0.984396**；
- F1：**0.979413**；
- ROC AUC：**0.989415**；
- Brier score：**0.023925**；
- 多数类基线 Accuracy：**0.751466**。

### 泄漏与输出

- 跨折重复特征向量：0；
- 分组重叠：0；
- 目标列未放入特征；
- 每折训练/测试规模已写入 `summary.json`；
- 输出 `predictions.csv`、`summary.json` 和 `manifest.json`。

验证批次：`20261031T141342Z-a0c7ef9b`。

### 置换重要性

独立测试折、5 次重复：

1. `convexity`：0.178974 ± 0.005653；
2. `compactness`：0.145181 ± 0.005763；
3. `elongation`：0.012265 ± 0.003302。

这是 permutation importance，不是 SHAP，也不证明因果。

## 当前边界

- 这些指标只描述该表、该特征和该分组方案下的实验；
- 高指标不等于跨区域或跨时期泛化；
- 标签质量、空间尺度和重复对象仍需独立审查；
- 安装与 Host 激活是不同状态，需按 DSH 提示重启或启用。
