# 技能与提示词

默认工作流从 MCP 读取多模模式，将技能目录加入探索提示词，再由模型按任务调用 `read_research_skill` 读取所需文档。模式内容来自数据库；不会把参考 schema 当作用户数据库的实际模式。

## 技能文档

| 文件 | 对应内容 | 加载方式 |
| --- | --- | --- |
| [技能目录](../backend/prompts/agent_research_report_flow/runtime_docs/skill_catalog.md) | 三类任务技能和通用教程的选择入口 | 探索提示词加载 |
| [基本多模数据获取](../backend/prompts/agent_research_report_flow/runtime_docs/basic_access_skill.md) | 论文 §4.2 的基本数据获取技能 | `read_research_skill("basic_access")` |
| [多模综合分析](../backend/prompts/agent_research_report_flow/runtime_docs/integrated_analysis_skill.md) | 机构比较、核心学者、趋势、合作和推荐 | `read_research_skill("integrated_analysis")` |
| [论文语义综合](../backend/prompts/agent_research_report_flow/runtime_docs/semantic_synthesis_skill.md) | 主题召回、候选补全、关系扩展与内容综合 | `read_research_skill("semantic_synthesis")` |
| [MMSQL 教程](../backend/prompts/agent_research_report_flow/runtime_docs/mmsql_tutorial_for_sql_agent.md) | 语法、模型操作、跨模型组合、示例、检查修正 | `read_research_skill("mmsql")` |

三类任务技能均按适用场景、所需数据、推荐步骤、可复用 MMSQL 方案、结果检查规则、停止条件六部分组织，对应论文式 (43)。MMSQL 教程的五部分对应式 (44)。

## 主方法提示词

| 文件 | 作用 |
| --- | --- |
| [对话入口](../backend/prompts/agent_research_report_flow/runtime_prompts/conversation_agent.md) | 理解历史上下文、决定是否启动研究 |
| [数据探索](../backend/prompts/agent_research_report_flow/runtime_prompts/research_agent.md) | 模式理解、技能选择、计划与查询迭代、有效结果确认 |
| [最终复核](../backend/prompts/agent_research_report_flow/runtime_prompts/selection_review.md) | 探索结束但尚未 finalize 时的一次补充确认 |
| [报告生成](../backend/prompts/agent_research_report_flow/runtime_prompts/report_agent.md) | 从最终有效观察生成报告，无查询工具 |
| [多模证据生成](../backend/prompts/agent_research_report_flow/runtime_prompts/citation_agent.md) | 从同一有效集合选择和解释结构化证据，无查询工具 |

## 其他入口与参考

| 文件 | 使用范围 |
| --- | --- |
| [DB_Tools 探索提示词](../backend/prompts/agent_research_report_flow/runtime_prompts/research_db_tools.md) | 仅显式选择 `baseline_polystore` 的独立后端 |
| [兼容工具调用入口](../backend/prompts/main_agent.md) | `PromptProvider` 的兼容入口，默认研究流程不加载 |
| [CN schema 参考](../backend/prompts/agent_research_report_flow/runtime_docs/cn_schema_reference.md) | 可选 CN 适配参考，不自动注入探索；实际模式由 MCP 返回 |

本地观察选择工具的描述和参数定义位于 `app/agent_experiments.py::_report_selection_tool_definition`；结果分析工具定义位于 `app/research_observations.py`。各 MCP 工具说明随注册函数返回给模型，主方法见 `release_server.py` 与 `cn_queries.py`。工具描述、参数定义及执行结果共同构成模型可见的查询接口。

`BIGDIPPER_AGENT_PROMPTS_DIR` 和 `BIGDIPPER_AGENT_PROMPT_DOCS_DIR` 可分别指定提示词与技能目录。文件名保持表中约定；使用自定义目录时应提供完整文件。数据库连接、向量对象和模型配置由部署环境提供，不写入技能文档。
