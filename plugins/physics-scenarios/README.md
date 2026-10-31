# Physics Scenarios and Ensembles

用于可解释的解析物理场景，而不是替代 COMSOL、有限元或全耦合模型。

## `physics_scenarios`

所有结果都带 `scenarioOnly: true`。

### `thermal`

周期表面温度下的一维热传导解析解：

```text
T(z,t) = T_mean + A × exp(-z/d) × sin(ωt - z/d + phase)
d = sqrt(2κ/ω)
κ = k / (rho × c)
```

示例参数：

```json
{
  "action": "thermal",
  "conductivityWmK": 2.0,
  "densityKgM3": 2400,
  "heatCapacityJkgK": 960,
  "meanSurfaceC": 14.2,
  "amplitudeC": 12,
  "periodDays": 1,
  "depthsM": [0, 0.05, 0.1, 0.2],
  "timesDays": [0, 0.25, 0.5, 0.75, 1]
}
```

输出温度、热扩散率、阻尼深度、衰减系数和相位滞后。

### `consolidation`

Terzaghi 一维固结近似：

```text
S_ultimate = Cc / (1 + e0) × H × log10(sigma_final / sigma_initial)
Tv = Cv × t / H_drainage²
S(t) = U(Tv) × S_ultimate
```

输出最终沉降、固结度、剩余沉降和 `settlement + remaining - ultimate` 守恒残差。

### `ensemble`

对热学场景做参数集合：

```json
{
  "action": "ensemble",
  "scenario": "thermal",
  "baseParameters": {"depthM": 0.2, "timeDays": 0.5},
  "parameterRanges": {
    "conductivityWmK": [1.6, 2.4],
    "amplitudeC": [10, 14]
  },
  "samples": 100,
  "randomState": 42
}
```

输出每个样本和 P05/P50/P95 温度分位数。默认使用给定范围的**均匀抽样**，不自动声称该分布就是真实概率分布。

## 科学边界

- 解析热传导假设均质、半无限、周期性边界，不包含水化热、相变、对流、辐射或复杂几何；
- 一维固结近似不包含侧向变形、非线性压缩、蠕变或多层排水；
- 集合分位数只反映输入范围和公式，不是经过校准的概率风险；
- 这些场景适合参数检查、教学和敏感性分析，不替代现场监测或工程定值。
