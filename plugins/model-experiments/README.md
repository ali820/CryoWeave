# Model Experiments and Validation

用于科研表格的可复现模型实验：分组交叉验证、滚动回测、基线比较和置换重要性。

## `model_experiments`

### `evaluate`

```json
{
  "action": "evaluate",
  "projectRoot": "<project-root>",
  "table": "classifier_training.csv",
  "featureColumns": [
    "log_area_km2",
    "log_perimeter_km",
    "compactness",
    "convexity",
    "elongation"
  ],
  "targetColumn": "reference_label",
  "task": "classification",
  "positiveLabel": "lake",
  "groupColumn": "spatial_block",
  "model": "random_forest",
  "cvFolds": 5,
  "nEstimators": 100,
  "randomState": 42
}
```

分类输出：accuracy、precision、recall、F1、ROC AUC、Brier score；回归输出：MAE、RMSE、R²。

同时计算：

- 同一数据上的多数类/均值基线；
- 每折训练/测试规模；
- 跨折重复特征向量计数；
- 分组重叠计数；
- 目标列是否误放入特征。

输出 `predictions.csv`、`summary.json` 和 `manifest.json`。

### `backtest`

按 `orderColumn` 排序，采用扩展训练窗口：

- `initialTrainFraction` 默认 0.6；
- `testFraction` 默认 0.2；
- `stepFraction` 默认 0.2。

每折只用过去的数据训练，再预测后续块。顺序回测不等于时间外推能力保证。

### `importance`

在独立测试折上使用 `sklearn.inspection.permutation_importance`：

- `nRepeats` 默认 5；
- 分类评分默认 accuracy；
- 回归评分默认 R²；
- 输出特征重要性均值与标准差。

这是**置换重要性，不是 SHAP**，不表示因果效应。

## 科学边界

- 分组验证减少空间/对象泄漏，但不能自动证明跨区域泛化；
- 高指标可能受标签质量、样本重复、空间尺度和特征定义影响；
- 基线必须与模型使用同一验证口径；
- 该插件不自动训练最终生产模型，也不替代外部独立测试。
