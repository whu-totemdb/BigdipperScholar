# 数据库接入

## 1. 能力边界

本仓库提供应用层和 MCP 查询适配器，不创建、导入或实现论文中的数据库。默认协议为 PostgreSQL wire protocol。SQL 中出现的 MATCH、JSON 函数和向量运算必须由接入引擎提供。

配置不包含内网 IP、账号、密码和现网配置。`backend/.env.example` 所有外部服务身份均留空。使用自定义 MCP 时可以完全不配置 DB_*；由你的服务管理连接。

## 2. 通用 schema

`get_database_schema` 返回指定 schema/prefix 内可见的列及 `pg_indexes` 信息，并合并 `BIGDIPPER_SCHEMA_NOTES_PATH` 的 JSON。列数/索引数上限分别为 5000/2000，超出时返回 truncated=true，请缩小暴露范围。

图标签、向量维度/度量、文档结构与样例并非标准 PostgreSQL 元数据。部署者在 schema notes 中描述；Agent 不得把未声明能力当作可用能力。参见 [schema notes 示例](../examples/schema-notes.example.json)。

研究 Agent 首轮前调用 MCP 模式工具，将其记录为 observation 0；模式和技能观察只用于规划，不作为最终报告事实。Agent 根据问题读取 basic_access、integrated_analysis、semantic_synthesis 和 mmsql 技能，完成自由查询、纠错和结果选择。

任意表名都可以用于通用 SQL；为让图谱识别实体，请在 SELECT 时提供标准别名：

```sql
SELECT work_key AS paperid, paper_title AS title, publication_year AS year,
       document->>'abstract' AS abstract
FROM your_works
WHERE publication_year BETWEEN 2015 AND 2024;
```

此 SQL 仅示范列映射，需替换为实际模式。返回统计列（如 year/paper_count）可直接保存为统计证据，不必伪装成论文。

## 3. 语义检索

所有向量检索使用与关系、图和文档相同的 `DB_*` 连接。配置 `BIGDIPPER_VECTOR_TABLE`、`BIGDIPPER_VECTOR_ID_COLUMN` 和 `BIGDIPPER_VECTOR_COLUMN`，分别指定语料向量表、实体 ID 列和向量列。内置查询使用 `vector` 类型及 `<=>` 余弦距离算子；如表包含 schema，可写为 `public.work_vec`。

`search_papers_by_natural_language` 调用 embedding 服务生成查询向量，再在数据库中检索。`find_similar_papers_by_paperid` 直接从同一个数据库读取种子向量，查询相似论文并排除种子自身。两者返回实体 ID、实际距离、相似分数和排名，随后可用元数据工具或 MMSQL 补齐论文内容。

需要自定义自然语言检索 SQL 时，设置 `BIGDIPPER_SEMANTIC_SQL_PATH`。模板使用 psycopg2 命名参数 `%(embedding)s`、`%(limit)s`，可选 `%(query)s`。embedding 参数是 JSON 数组字符串，按数据库类型转换。模板不改变数据库连接，不启用外部向量库；种子相似检索仍使用配置的向量表。

参见 [向量 SQL 模板示例](../examples/semantic.pgvector.sql.example)。该文件展示用户数据库对象的映射方式。数据库提供对应类型和算子，应用不实现数据库引擎。模板由部署者维护，用户不能通过 HTTP 参数选择文件。

embedding 配置：

- `custom`：POST `<base>/embed`，请求 `{query}`，返回 `{embedding: [...]}` 或 `{vector: [...]}`。
- `openai`：兼容协议，POST `<base>/embeddings`，请求 `{input, model}`，返回 `data[0].embedding`。
- 模型、协议和维度来自配置；设置 `BIGDIPPER_EMBEDDING_DIMENSIONS` 后会校验维度，NaN/无限值会被拒绝。
- 语料 embedding 必须提前在外部数据库中生成并索引。此仓库没有离线导入/索引构建任务。

工具记录实际 score/distance、SQL、query、embedding 模型及维度。query-to-paper 召回只是检索证据，不生成不存在的 paper-to-paper 相似边。

## 4. CN 适配

`BIGDIPPER_DATA_PROFILE=cn` 开放专用工具，详见 `app/mcp_servers/release_server.py::CN_TOOLS`。查询实现集中于 `cn_queries.py`，仅访问统一数据库，依赖 `cn_papers`、`cn_paperdetails`、`cn_paper_author_affiliation`、`cn_author_details`、`cn_affiliations`、`cn_paper_ref` 等对象及相应函数；图工具依赖 MMSQL 图对象。CN 的默认向量映射为 `cn_paper_embeddings_full_hnsw(paperid, embedding)`、768 维，可以使用上述配置替换。

venue 工具依赖 `cn_sources`、来源关联、别名映射和 `cn_paper_venue_lookup`。不同 venue_key 不自动合并。schema 参考位于 `backend/prompts/agent_research_report_flow/runtime_docs/cn_schema_reference.md`，其中列出的索引只描述参考部署，不保证用户库已创建。

前端渲染从已保存证据生成图谱，不依赖再次查询文献库。用户主动的节点扩展可单独配置 Neo4j 展示服务，使用 `BIGDIPPER_NEO4J_ENABLED`、`BIGDIPPER_NEO4J_URI`、`BIGDIPPER_NEO4J_USER`、`BIGDIPPER_NEO4J_PASSWORD`。展示服务不注册为主方法的 MCP 查询工具，交互扩展也不改变原报告的有效观察。手动论文搜索和部分扩展依赖各自适配器的实体结构；任意 schema 的主要入口是 Agent 工作区。

## 5. 自定义 MCP

复制 [HTTP MCP 示例](../examples/mcp_servers.http.yaml.example) 到本地配置并设置 `BIGDIPPER_MCP_CONFIG`。默认 stdio 子进程使用后端当前 Python 解释器。

服务需提供模式发现、数据查询，以及适合任务的封装工具。保持统一实体 ID，返回成功/失败状态和来源。默认工作流仅调用实际注册工具，schema 发现失败会终止研究，不会读取参考库猜测结果。

主方法的自定义 MCP 必须将所有数据查询接入同一个多模数据库；不要把独立的向量库或图数据库查询注册到该配置。embedding 只生成向量，技能和观察标记是本地工作流功能。

只读会话和 SQL 检查是附加防护，不替代数据库权限。禁用可写高权限函数和不必要对象访问；图加载、索引创建等管理操作应在 Agent 运行前由数据库部署完成。

## 6. 独立 DB_Tools 后端

需要分别访问关系、图和向量服务时，使用 [独立配置示例](../examples/mcp_servers.db-tools.yaml.example)，设置 `BIGDIPPER_DB_TOOLS_MCP_CONFIG`，在研究请求中显式选择 `agentMode=baseline_polystore`。该后端使用 `db_tools_server.py` 和单独的探索提示词，可按需配置 Milvus、Neo4j 与 embedding 服务。

它不由默认聊天主方法加载，也不会在库内查询失败时自动启用。该入口保留多数据库工具的替代实现，不等同于论文实验配置的完整复现。Milvus 使用 `BIGDIPPER_MILVUS_URI`、`BIGDIPPER_MILVUS_COLLECTION`、`BIGDIPPER_MILVUS_VECTOR_FIELD`、`BIGDIPPER_MILVUS_PAPERID_FIELD`；图服务使用上述 Neo4j 配置，全部连接信息由部署者在本地填写。
