# 论文方法与代码

本仓库包含论文第 4 节的应用层流程。第 2—3 节所述数据库内核由外部统一多模数据库提供，应用通过 MCP 访问其查询能力。

| 论文要求 | 本仓库实现 | 边界 |
| --- | --- | --- |
| §2–3 四模型统一组织、MMSQL 与跨模型优化 | MCP 向外部 MMDB 提交查询，保留查询与执行结果 | 数据库引擎、算子、拓扑缓存、索引和数据导入不在本仓库 |
| §4.2 从 MCP 获取模式 | release_server.get_database_schema + research schema bootstrap | 关系字段/索引可发现；图/向量能力及样例由 schema notes 或自定义 MCP 提供 |
| §4.2 任务理解与技能选择 | research_agent、read_research_skill、三类任务技能及 MMSQL 教程 | 任务技能含场景、数据、步骤、查询方案、检查、停止六部分；教程含语法、模型操作、组合、示例、修正五部分 |
| §4.3 迭代执行、错误修正、更新计划 | AgentExperimentService._run_research，工具返回结果进入后续轮次 | 最大轮数/服务超时限制探索，不保证任意任务都达到完整性 |
| §4.3 完整研究过程 | events.jsonl，成功/失败调用、行动说明与显式关联观察 ID 的结果分析 | 查询前说明与查询后分析分开；引用已排除观察的联合分析不传给生成阶段 |
| §4.3 有效结果标记 | select_report_observations 的暂存/撤销/替换及最终确认，has_data 校验 | 相关性/非冗余性由模型判断；失败/空/schema/skill 由代码排除；最终复核失败时停止生成 |
| §4.4 报告智能体 | _generate_report，仅输入有效观察 | 不提供查询工具；最终回答直接返回该报告 |
| §4.4 证据智能体 | _generate_evidence_bundle，observedEvidence ID 选择与说明 | 不提供查询工具，与报告共用同一观察集，代码校验结构来源 |
| §4.4 多模证据与来源 | evidence_provenance + EvidenceGraphBuilder + evidence API | 只使用观察中存在的实体、字段和关系；无生成阶段补查 |
| 图1 任务相关子图 | GraphCanvas、节点/边详情、工作区持久化 | 布局可变，事实集合不因布局改变；缺少引文/语义数据则不造边 |

查询层提供工具封装、并行调用和短期缓存。主方法的 MCP 入口为 `release_server`，使用 `cn_queries` 和 `mmdb_vectors`，所有数据查询使用同一多模数据库连接。语义查询由 embedding 服务生成向量，随后在库内检索；种子相似查询直接读取库内向量。主入口不导入外部图/向量检索模块。

`baseline_polystore` 使用单独的 `BIGDIPPER_DB_TOOLS_MCP_CONFIG` 和 `research_db_tools.md`，保留分别调用关系、Milvus 和 Neo4j 工具的替代后端，不是默认主方法。未配置时明确报错，不静默切换。此配置仅分离查询后端，不表示复现论文中 ReAct+DB_Tools 的全部实验设置。

证据展示与研究查询分开。确定性证据构建只消费最终观察；前端渲染和用户主动的节点扩展可使用 Neo4j 等展示服务。交互扩展结果不回填为原研究任务的有效观察。

证据中的事实字段与关系由有效观察构建，模型可以补充证据支持哪些结论的说明。来源校验约束节点、关系与返回数据之间的对应，但不能替代用户对自然语言分析的判断。
