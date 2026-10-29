# 调研结果存放约定

调研过程、原始证据、含私有笔记标题的评测输出**不进本 Git 仓库**。仓库只保留可运行代码、设计定稿与可公开结论摘要。

## 推荐落点

与仓库同级的私有目录（已就位）：

```
<skills>/awam-knowledge/                 ← 本仓库（可推远程）
<skills>/awam-knowledge.private/         ← 不进 Git
  README.md
  queries-t18.json                       ← 真实 26 条 query 集
  research/                              ← 07–11 报告 + evidence/*
```

也可改放到加密盘 / Bitwarden Files / 自有备份仓，原则相同：**与公开仓物理隔离**。

## 分层

| 层 | 放哪 | 例子 |
|---|---|---|
| 可复现脚本 | 本仓 `code/` | `eval-recall.js`、`import-md-dir.js` |
| 设计定稿 / 产品结论 | 本仓 `design/`、`docs/`、`progress/` | 方案 v1.5、OPEN-ISSUES 结论句 |
| 调研过程与原始数字 | **仓外** `awam-knowledge.private/research/` | 预注册、`out-*.json`、宿主探测笔记 |
| 真实评测集 | **仓外** `queries-t18.json` | 含私有 `expect` 路径的 26 条 |

## 本机评测怎么跑

仓库内 `code/queries-t18.json` 是**脱敏示例**（格式相同，条数更少）。对真实语料评测时显式指向私有集：

```bash
node code/eval-recall.js \
  --queries-json ../awam-knowledge.private/queries-t18.json \
  --topn 50 --md-out out.md > out.json
```

可选：把真实集复制为 `code/queries.local.json`（已 ignore），用 `--queries-json code/queries.local.json`。

## 写回仓库时只写结论

- 允许：一句收口结论、公开指标数量级、决策（例如「BM25 标题×4 为主」）。
- 禁止：私有笔记标题、完整 evidence JSON、本机绝对路径、扩展词典、宿主探测原始日志。
- 新调研默认先落 `awam-knowledge.private/research/`；确认可公开后再把**摘要**补进 `docs/` 或 `progress/`。
