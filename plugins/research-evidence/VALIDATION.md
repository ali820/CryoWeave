# Research Evidence 验证记录

验证日期：2026-10-30。所有真实项目检查均为只读，未修改研究文件。

## 离线检查

- `python scripts/check_repo.py`：通过，覆盖仓库 JSON、Python 和 Bundle 必需资源。
- `node --check plugins/research-evidence/index.js`：通过。
- `python plugins/research-evidence/evidence_worker.py --self-test`：通过，6 项检查，包括正常 fixture、篡改后的 `SHA256_MISMATCH`、相对路径逃逸和根目录越权。
- Host `defineTool` 注册及参数 Schema：通过，工具名 `research_evidence_audit`。
- 额外边界验证：stdin 4 MiB 上限、畸形/空 JSON、哈希超限、严格 resolved 状态、finding 截断和空 checks。

## 20-qxp 真实只读审计

项目：`E:/文章同步/20-qxp/qxp-lake-infrastructure-exposure`，执行全部四类检查。

- 扫描 2,817 个文件，未截断；
- 找到并读取 `CURRENT_AUTHORITY.md`，标题与更新日期提取正常；
- 解析 50 份图件导出清单，验证 178 个文件引用；
- 发现 72 个 `EXTERNAL_ABSOLUTE_PATH`：旧 P5 清单仍指向历史根目录 `E:\文章`，当前项目为 `E:\文章同步`，插件未读取这些外部路径；
- 发现 1 个 `INVALID_EXPORT_JSON` 和 4 个 `BYTES_MISMATCH`（候选图件清单结构及未声明字节数）。

这些问题证明审计能区分当前有效文件与历史路径/结构问题；不表示所有 50 份清单均无效。

## 15 双论文工作区真实只读审计

项目：`E:/文章同步/15-protect_permafrost/5-双论文工作区`，执行 `claims` 和 `conflicts`。

- 扫描 1,157 个文件，未截断；
- 读取 2 份主张表，共 27 条主张；
- `CLAIM_GATE_BLOCKED` 7 条，`CLAIM_STATUS_BLOCKED` 7 条，保留门控与证据状态两类记录；
- 读取 3 份数字冲突表，共 12 条，其中 9 条 `resolved`、3 条 `resolved_by_definition`，未解决数为 0。

## Host 与编码验证

通过实际 Host 工具入口分别运行 20 和 15 审计。中文项目根目录、中文 finding 路径和中文证据状态均以 UTF-8 正确返回，未出现模块解析或 JSON 协议错误。

## 当前限制

- 未自然语言抽取论文数字；
- 未检查 DOCX/PDF 正文、图片视觉内容或全部历史归档；
- `resolved` 仅表示项目登记表中的状态，不是插件独立复算；
- 安装和 Host 激活是不同状态，当前会话是否能看到工具取决于 DSH 重启/加载结果。
