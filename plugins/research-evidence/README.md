# Research Evidence Audit

只读科研证据审计插件。它让 Agent 直接检查项目中的结构化证据，不修改文章、数据或图件，也不访问网络。

## `research_evidence_audit`

输入绝对 `projectRoot`，默认执行四类检查：

| 检查 | 当前能力 |
| --- | --- |
| `authority` | 查找 `CURRENT_AUTHORITY.md`，返回路径、标题、更新日期、大小、修改时间和 SHA-256 |
| `figures` | 查找 `*.export.json`，验证 `source_data`、`cache_assets` 和 `outputs` 中的文件存在性、字节数和 SHA-256 |
| `claims` | 检查 `claim_evidence_map*.csv` 的主张 ID、正文、来源字段和重复项，汇总证据状态与门控状态 |
| `conflicts` | 汇总 `numeric_conflict_register.csv`，区分已解决和未解决数字冲突 |

返回 JSON，包含 `summary`、`findings` 和 `details`。`auditStatus: clean` 表示本次结构化检查未发现问题；`issues_found` 表示存在需要人工审阅的项目。研究问题不会把工具执行本身变成失败。

## 示例

```text
用 research_evidence_audit 审计
E:\文章同步\20-qxp\qxp-lake-infrastructure-exposure，
检查权威文件和图件清单，列出所有哈希不一致或缺失文件。
```

```text
审计 E:\文章同步\15-protect_permafrost\5-双论文工作区，
只执行 claims 和 conflicts，汇总锁定主张与未解决数字冲突。
```

对应参数示例：

```json
{
  "projectRoot": "E:/文章同步/15-protect_permafrost/5-双论文工作区",
  "checks": ["claims", "conflicts"],
  "maxFindings": 100
}
```

## 范围和安全

- 只能读取 `cordis.patch.yml` 中 `allowedRoots` 内的项目；
- 默认跳过依赖、缓存、归档和临时目录；
- 默认最多扫描 20,000 个目录项、返回 100 条 finding、哈希单文件 256 MiB；
- 跳过符号链接逃逸和引用路径逃逸；
- 不执行项目脚本，不修改源文件，不读取认证文件。

## 当前不做什么

- 不自然语言抽取论文中的全部数字；
- 不判断哪个科学结论正确；
- 不把 `resolved` 当作独立复算证明；
- 不检查图片视觉内容、DOCX/PDF 正文或全部历史归档；
- 不自动接受旧稿件、模拟审稿或弱标签作为独立证据。

这些能力可在后续版本中按明确格式和验收标准增加。
