# GEE 遥感数据分析插件

让 DeepSeek Harness Agent 直接执行受约束的 Google Earth Engine 分析，并返回结构化 JSON 结果。插件不执行模型提供的任意 Python 或 JavaScript。

## 工具

### `gee_status`

检查隔离 Python 环境、Earth Engine API 初始化、Cloud 项目配置和一次最小在线请求。不会输出凭据内容。

### `gee_analyze`

支持以下 `operation`：

| 操作 | 用途 |
| --- | --- |
| `collection_summary` | 统计影像数量、首尾时间和样例影像 ID |
| `region_stats` | 对影像集进行中位数/均值等合成，再计算区域波段统计 |
| `timeseries` | 计算任意影像集指定波段的区域时间序列 |
| `ndvi_timeseries` | 计算 Sentinel-2 NDVI 时间序列及有效像元数 |
| `ndvi_stats` | 计算 Sentinel-2 中位数 NDVI 合成影像的区域统计 |

区域可以传入 GeoJSON `geometry`、Earth Engine FeatureCollection `geometryAsset`，或传入 `longitude`、`latitude` 和可选的 `bufferMeters`。`geometryAsset` 例如 `projects/ee-my-road/assets/dukuregion`；资产和点坐标均可按需设置缓冲区。日期采用 `startDate` 包含、`endDate` 不包含的区间。

NDVI 固定使用 `COPERNICUS/S2_SR_HARMONIZED`，默认过滤 `CLOUDY_PIXEL_PERCENTAGE > 60` 的影像，并通过 SCL 与 QA60 掩膜云、云影、卷云和雪。该掩膜适合通用分析，不应替代针对具体研究区的质量评估。

### `gee_corridor_analyze`（CryoWeave 1.1 新增）

直接读取 `corridor_analysis` 生成的 `units.geojson`，对每个 `segment_id × ring_id` 计算 Sentinel-2 中位数 NDVI 和有效像元数，输出 `ndvi_zonal.csv` 与 manifest。

```json
{
  "table": "state://<corridorBatchId>/units.geojson",
  "unitIds": ["seg_0001", "seg_0002", "seg_0003"],
  "ringIds": ["ring_01"],
  "startDate": "2024-06-01",
  "endDate": "2024-09-01",
  "imageLimit": 10,
  "scale": 30,
  "stat": "mean"
}
```

- `unitIds`、`ringIds` 可筛选子集，`limit` 默认 60、最大 500；
- `imageLimit` 用于限量验证，正式合成时应明确是否需要限制；
- GEE 与走廊插件共用 `stateRoot`，所以可直接复用走廊返回的 `state://` 路径；
- 输出路径可交给 `corridor_analysis summarize`；向 `research-run` 登记时使用 `state://corridor/<batchId>/ndvi_zonal.csv`；
- 分区和 NDVI 计算均在 GEE 云端执行，本地只写入小型 CSV 和 manifest。

## Python 环境

插件使用独立 `.venv`，不修改全局 Python。重建环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`cordis.patch.yml` 的 `pythonPath` 必须指向该环境。若移动插件目录，需要同步更新此路径。

## Earth Engine 认证与项目

插件不会自动创建 Google Cloud 项目或授予 Earth Engine 权限。需要：

1. 一个已启用 Earth Engine API 的 Google Cloud 项目；
2. 当前用户或服务账号具有相应访问权限；
3. 在插件所用 Python 环境中完成认证。

用户认证可在该虚拟环境中运行：

```powershell
.\.venv\Scripts\python.exe -m pip install earthengine-api
.\.venv\Scripts\earthengine.exe authenticate
```

然后在 `cordis.patch.yml` 中将 `project` 设置为 Cloud 项目 ID，也可以设置 `DSH_GEE_PROJECT` / `GEE_PROJECT` / `GOOGLE_CLOUD_PROJECT` 环境变量，或在 `gee_status`、`gee_analyze`、`gee_corridor_analyze` 调用中临时传入 `project` 参数。也可以使用标准的 `GOOGLE_APPLICATION_CREDENTIALS` 服务账号认证方式；不要把私钥或凭据内容写入对话、YAML 或插件源码。

## 安装

在 DSH Web 侧边栏打开 **插件 → 添加插件 → 安装第三方插件**，输入本目录的绝对路径并安装。安装与激活是不同状态；若管理器返回 `restart-required`，需要重启 DSH。

## 示例

完成认证后，可直接对 Agent 说：

```text
先用 gee_status 检查环境，然后分析 2025 年 1 月到 6 月，
东经 116.4、北纬 39.9 周边 10 公里的 Sentinel-2 NDVI 时间序列，
尺度 30 米，最多 100 景，并总结变化趋势。
```

对应工具参数的核心部分为：

```json
{
  "operation": "ndvi_timeseries",
  "longitude": 116.4,
  "latitude": 39.9,
  "bufferMeters": 10000,
  "startDate": "2025-01-01",
  "endDate": "2025-07-01",
  "scale": 30,
  "limit": 100,
  "stat": "mean"
}
```

## 限制

- 单次时间序列最多返回 500 景，默认 100 景；默认丢弃统计值为空的行。
- 默认区域统计尺度为 30 米，NDVI 为 10 米；可自行调整。
- Host 默认超时 240 秒，可配置为 1–600 秒。
- 不执行任意 Earth Engine Python/JavaScript，不创建导出任务，不写入 Google Drive 或 Cloud Storage。
- GEE 配额、数据可用性、影像质量和研究设计仍需单独核验。

## 本次独库项目验证

本机默认项目已配置为 `ee-my-road`，并复用 `projects/ee-my-road/assets/dukuregion` 完成真实在线调用：

- GEE 状态检查和 Host 工具入口检查通过；
- 2024 年 Landsat 9 Collection 2 Level 2 覆盖清单返回 226 景；
- 2024-06-01 至 2024-09-01 的 Sentinel-2 查询返回 458 景，取前 50 景进行功能验证合成，30 m 区域平均 NDVI 为 0.3528391450；
- 通用 Landsat 区域统计、通用时间序列和 Sentinel-2 NDVI 时间序列均返回预期结构化记录。

上述限量合成与时间序列用于验证插件链路，不应直接作为论文最终结果。正式分析应明确影像筛选、完整时间窗口、云质量策略和研究区定义。
