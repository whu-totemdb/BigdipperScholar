# 部署与运行

本页补充 [README](../README.md) 的启动步骤。命令以 Linux/Bash 为例，后端使用仓库根目录的 `.venv` 环境。

## 服务组成

| 服务 | 默认地址或启动方式 | 用途 |
| --- | --- | --- |
| React 前端 | `127.0.0.1:5173` | 工作区、报告与证据图谱 |
| FastAPI 后端 | `127.0.0.1:8000` | 用户会话、智能体执行、结果存储 |
| 内置 MCP | 后端启动 stdio 子进程 | 发现数据库模式、执行查询；无需单独开放 HTTP 端口 |
| 数据库、LLM、embedding | 用户配置 | 文献查询、模型推理与可选语义检索 |

先准备数据源和模型服务，再启动后端、前端。未配置 embedding 时，可使用其他已接入的查询能力；调用语义检索工具会返回配置错误。

## 配置说明

后端读取 `backend/.env`。配置示例不包含实际服务地址或凭据。

| 配置项 | 说明 |
| --- | --- |
| `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` | 模型接口、密钥与模型名称；接口地址保留服务要求的 API 前缀 |
| `DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_NAME` | 内置 MCP 使用的数据库连接信息 |
| `DB_SSLMODE` | 数据库连接加密模式，按部署环境设置 |
| `BIGDIPPER_DATA_PROFILE` | 默认 `generic`；`cn` 额外启用依赖兼容表结构的专用工具 |
| `BIGDIPPER_DB_SCHEMA`、`BIGDIPPER_TABLE_PREFIX` | 模式发现的 schema 与表名前缀；前缀过滤不替代数据库权限 |
| `BIGDIPPER_SCHEMA_NOTES_PATH` | 图、文档、向量能力的补充说明文件 |
| `BIGDIPPER_VECTOR_TABLE`、`BIGDIPPER_VECTOR_ID_COLUMN`、`BIGDIPPER_VECTOR_COLUMN` | 同一多模数据库中的语料向量表、实体 ID 列和向量列 |
| `BIGDIPPER_MCP_CONFIG` | 自定义 MCP 配置路径；默认使用 `backend/mcp_servers.yaml` |
| `BIGDIPPER_DB_TOOLS_MCP_CONFIG` | 可选的独立 DB_Tools 配置，仅用于显式选择的替代后端 |
| `BIGDIPPER_DB_PATH` | 账号、工作区、对话和证据的 SQLite 路径，示例值为 `runtime/workspaces.db` |
| `BIGDIPPER_AGENT_EXPERIMENT_LLM_TIMEOUT` | 单次模型请求超时，示例配置为 120 秒 |
| `BIGDIPPER_DB_STATEMENT_TIMEOUT_MS` | 单条数据库语句超时，默认 60000 毫秒 |
| `BIGDIPPER_DB_CONNECT_TIMEOUT_SECONDS` | 数据库连接超时，默认 5 秒 |
| `BIGDIPPER_RESEARCH_REPORT_CHAT_MAX_ROUNDS` | 探索轮数上限，默认 8 |

路径建议填写绝对路径；若使用相对路径，应固定从 `backend/` 启动后端。修改后端配置后需重启后端进程。

语义检索的 embedding 协议、模型、维度与 SQL 模板见 [语义检索配置](data-integration.md#3-语义检索)。查询向量与语料向量须来自相同模型及一致的维度。

## 调整服务端口

例如将后端改为 `8010`：

```bash
# 从 backend/ 执行，并先激活虚拟环境
uvicorn app.main:app --host 127.0.0.1 --port 8010
```

前端开发服务应同时设置代理目标：

```bash
# 从 frontend/ 执行
BIGDIPPER_API_TARGET=http://127.0.0.1:8010 npm run dev -- --host 127.0.0.1 --port 5173
```

`BIGDIPPER_API_TARGET` 控制开发代理。`frontend/.env` 的 `VITE_API_BASE_URL` 默认留空，浏览器使用同源 `/api`；通常无需修改它。

## 前端静态构建

```bash
cd frontend
npm ci
npm run build
```

构建产物位于 `frontend/dist/`。部署到 Web 服务器时，需要同时配置：

- 静态文件根目录指向 `dist/`，前端页面路径回退到 `index.html`。
- `/api` 请求转发到 FastAPI 后端，保留原请求路径。
- 对流式接口关闭代理缓冲，并允许研究任务所需的连接时长。

Vite 的开发代理不会写入静态产物；只上传 `dist/` 不会自动接通后端。使用同源部署时保持 `VITE_API_BASE_URL` 为空；如修改构建时环境变量，需要重新构建前端。

公网部署还需配置 HTTPS、访问控制及服务进程管理。当前账号与会话功能面向研究系统部署。

## 结果与运行记录

SQLite 保存账号、工作区、对话和证据，不承担文献数据库的功能。请为它配置持久化路径，并纳入备份。

每次研究的日志默认保存于 `backend/logs/research_report_chat/<run_id>/`，包含查询、返回结果、状态及生成内容。可使用 `BIGDIPPER_RESEARCH_REPORT_CHAT_LOG_DIR` 调整目录。日志可能包含用户问题与数据库内容，应按自己的数据管理要求保存；`.env`、运行数据和日志均已加入 Git 忽略规则。

完整证据可通过已登录用户的接口读取：

```text
GET /api/chat/runs/{run_id}/evidence?workspace_id=...
```

该接口返回报告、有效观察与完整证据。图谱有展示节点上限，完整证据接口不受该展示上限裁剪。

## 常见问题

| 现象 | 检查位置 |
| --- | --- |
| `preflight` 报缺少配置 | 检查 `backend/.env` 的模型配置；使用内置 MCP 时还需数据库配置 |
| `preflight --probe` 失败 | 检查 MCP 是否启动、数据库网络与权限、schema 是否可访问；自定义 MCP 还需核对模式发现接口 |
| 页面可打开，API 请求失败 | 检查后端进程、端口、前端开发代理或静态站点反向代理 |
| 模型请求失败 | 检查 API 前缀、模型名称、密钥与工具调用支持；模式探测成功不代表模型接口已验证 |
| 语义检索失败 | 检查 embedding 协议、模型与维度、查询模板路径及数据库向量算子 |
| 能生成报告，但缺少作者或引文关系 | 检查查询结果是否返回实体 ID、署名或引文端点；参考证据协议设置字段别名 |
| 任务长时间未结束 | 查看对应 run 的日志，定位模型调用或数据库查询；按需检查 SQL 执行计划与索引 |
| 手动论文搜索不可用 | 检查专用接口依赖；通用数据源请使用 Agent 工作区 |

任务取消可能需要等待当前工具调用或模型请求结束。不要仅因页面等待就重复提交同一任务；先检查日志中的执行状态。
