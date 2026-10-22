# 观察与证据协议

每次工具执行记录 observationId、round、toolName、arguments、status、result/error、latencyMs 和 execution。完整日志同时保留失败与空结果；有效选择器拒绝把它们交给报告/证据生成。

探索 Agent 的工具响应同时包含 `execution`，可直接查看本次实际 SQL/MMSQL，不必额外调用查看工具。`queryText` 保存查询，`parameters` 保存绑定值；多阶段工具保留 `statements` 或种子读取的 `seedQuery`。向量绑定值仅提供维度、模型、来源和摘要哈希，其他查询参数保留实际值。参数化查询失败时同样返回查询信息，供下一轮修正。自定义 MCP 也应按此约定返回 `_execution`；未返回的 SQL 不会由应用猜测补齐。

探索中的选择是暂存集合，支持追加、撤销和替换。结束时 `select_report_observations(mode="finalize")` 提交完整最终集合，空数组表示没有有效证据；成功确认后直接进入生成阶段。最终确认不能与新查询同时提交。遗漏确认或达到轮数上限时，只进行一次补充复核；复核失败则停止生成，不将暂存集合视为最终依据。

查询前的发言保存为 `actionRationale`，用于复核行动目的，不作为结果分析交给生成阶段。查询后的分析显式记录为 `resultAnalyses`，每项包含 `sourceObservationIds`、`summary` 和 `recordedAtRound`；可随下一轮查询或最终确认一起记录，不需要单独增加模型轮次。引用尚未返回的观察会被拒绝。联合分析只有在其所有来源都进入最终集合时才传给报告与证据智能体，其余分析仍保留在完整日志中。分析文字是模型解释，事实与数值以原始查询结果为准。

为避免重复文本开销，报告与证据模型的输入中每段联合分析只发送一次，并保留全部关联 ID；原始查询结果不裁剪，持久化观察中的分析关联也不删除。

推荐工具响应：

```json
{
  "status": "success",
  "row_count": 1,
  "data": [{"paperid": "P1", "title": "An observed work", "year": 2024}],
  "_execution": {
    "backend": "mmdb",
    "queryLanguage": "mmsql",
    "queryText": "SELECT ...",
    "databaseLatencyMs": 12.3,
    "rowCount": 1
  }
}
```

MCP TextContent JSON、普通 JSON、嵌套 data/rows/papers/nodes/edges 列表都可解包。对于数据库结果列中的嵌套 JSON，适配器应返回 JSON 对象/数组或投影成平面行；不可把未知格式当作已验证关系。

| 返回事实 | 字段 |
| --- | --- |
| 论文/摘要 | paperid（或 paperId/paper_id）、title、year、abstract、doi、cited_by_count |
| 署名 | paperid、authorid、author_name；或 authors 对象数组 |
| 署名机构 | 同一行的 paperid、authorid、institutionid、institution_name |
| 引文 | citing_paperid、cited_paperid，保留真实方向 |
| 通用图节点 | id、nodeType（paper/author/institution/field）、label |
| 通用图边 | source、target、type（cites/reference/cited_by/similar/authored_by/affiliated_with/coauthored_with/institution_collaboration/has_field）；端点须在有效结果出现 |
| 语义 | paperid、score 或 similarity 或 distance、rank；种子论文相似工具还须提供 paper_id 参数 |
| 统计 | 任意明确命名的聚合列，SQL/参数保留统计口径 |

每个证据单元具有 `evidenceId=obs-<observationId>-row-<index>`、evidenceType、content、sourceObservationIds 和 sources。sources 包含工具、参数、原查询及执行元数据。实体、关系、文档、语义、统计证据均保留原数据行。

证据 Agent 基于完整有效观察和 observedEvidence 选择 evidenceId 并添加 supportingClaims。确定性校验器不接受它创造的属性、节点或关系；事实由原始观察重建。未被 Agent 注释但属于有效观察的行仍保留，避免丢失来源。supportingClaims 是模型解释，不是额外数据库事实。

展示用 field 节点中的 `evidenceType=observation` 表示一次真实探索来源，不能解释为论文领域标签。单次 query 召回多篇论文不意味着论文之间彼此相似；共同作者是可追踪到 authored_by 的派生投影视图，并非额外的引用/语义关系。

只有作者名称没有作者 ID 时，展示 ID 按原始标签生成，并标记 identityBasis=observed_label；这不是作者消歧。生产数据宜提供真实统一 ID。

图谱最多展示 200 个节点（默认 80），超出会警告；完整证据与有效观察可从授权的 evidence API 获取。生成或刷新证据图不查询外部数据库，避免改变支持报告的证据集合。
