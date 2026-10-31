# Field Multisource Observations

用于现场地温、UAV、GPR、RTK 等多源观测的表格检查、时空配对、质量控制和误差比较。

## `field_multisource`

支持 CSV、XLS、XLSX 和表格 JSON。Excel 可指定 `sheetName` 与零基 `headerRow`。

### `inspect`

```json
{
  "action": "inspect",
  "projectRoot": "<project-root>",
  "source": "data/temperature.xls",
  "sheetName": "9A",
  "headerRow": 2,
  "timeColumn": "Unnamed: 0",
  "valueColumns": ["0.5"]
}
```

返回行数、列数、时间范围、数值统计和样例。

### `qc`

```json
{
  "action": "qc",
  "projectRoot": "<project-root>",
  "source": "data/temperature.xls",
  "sheetName": "9A",
  "headerRow": 2,
  "timeColumn": "Unnamed: 0",
  "valueColumns": ["0.5"],
  "minValue": -50,
  "maxValue": 50,
  "maxGapHours": 72
}
```

检查缺失、越界、重复和时间间隔。`qaStatus` 只表示当前规则下的 QA 结果。

### `align`

按时间最近邻，或结合经纬度进行时空配对：

```json
{
  "action": "align",
  "projectRoot": "<project-root>",
  "sourceA": "upstream.xls",
  "sourceB": "downstream.xls",
  "sourceASheet": "9A",
  "sourceBSheet": "11A",
  "headerRow": 2,
  "timeColumnA": "Unnamed: 0",
  "timeColumnB": "Unnamed: 0",
  "valueColumnA": "0.5",
  "valueColumnB": "0.5",
  "maxTimeDiffSeconds": 0
}
```

如果提供 `latColumnA/B` 与 `lonColumnA/B`，还会计算球面距离并应用 `maxDistanceMeters`。输出 `pairs.csv` 和 manifest，保留原始行索引、时间差、距离和配对值。

### `compare`

```json
{
  "action": "compare",
  "projectRoot": "<project-root>",
  "table": "state://<batchId>/pairs.csv",
  "columnA": "valueA",
  "columnB": "valueB"
}
```

返回样本数、MAE、RMSE、偏差、最大绝对误差、相关系数和 R²。

## 科学边界

- 不同站点、不同深度或不同传感器之间的差异，不等于传感器精度；
- 配对误差依赖时间窗口、空间距离和观测代表性；
- QA 通过不代表观测一定无系统偏差；
- 该插件不替代现场标定、仪器比对或人工异常复核。
