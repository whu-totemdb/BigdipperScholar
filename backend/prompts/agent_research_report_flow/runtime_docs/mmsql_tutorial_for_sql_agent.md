# MMSQL 查询教程

本文提供基本语法、四模型操作、跨模型组合、查询示例、检查与修正方法。示例使用论文中的 work、work_doc、work_vec、work_v 等概念名称；执行前通过 MCP 模式将其映射为当前数据库对象。所有查询在同一多模数据库中执行。

## 1. 基本语法与查询结构

MMSQL 在 SQL 的选择、连接、聚合与排序结构中组合图模式、文档访问和向量运算。典型图查询结构为：

```sql
SELECT w.id AS paper_node_id, a.id AS author_node_id
FROM scholarly_graph
MATCH {(w: work_v)-[e: work_author_e]->(a: author_v)}
WHERE w.id = 10001;
```

`FROM` 指定图对象，`MATCH {...}` 声明顶点标签、边标签及方向，`WHERE` 过滤实体或属性，`SELECT` 返回所需字段。SQL 的 `WITH` 可以组织子查询，将不同模型的结果通过统一实体 ID 连接。

查询中的图对象、标签、边方向及顶点 ID 与业务 ID 的映射以模式为准。图的整数内部 ID 不一定就是论文对外的字符串 ID。

## 2. 四类模型操作

### 2.1 关系：过滤、连接与聚合

```sql
SELECT publication_year AS year, COUNT(DISTINCT work_id) AS paper_count
FROM work
WHERE publication_year BETWEEN 2015 AND 2024
GROUP BY publication_year
ORDER BY publication_year;
```

连接署名或主题表后，一篇论文可能出现多行。按论文统计时使用统一论文 ID 去重。聚合指标与展示 Top-k 分开定义。

### 2.2 图：模式匹配与结构查询

```sql
SELECT p.properties->>'work_id' AS citing_paperid,
       r.properties->>'work_id' AS cited_paperid
FROM scholarly_graph
MATCH {(p: work_v)-[e: work_referenced_work_e]->(r: work_v)}
WHERE p.properties->>'work_id' = 'W123';
```

此例返回种子论文引用的对象；查被引论文时过滤 `r` 端。署名、合作和主题关系使用各自的标签与方向。多步路径可以组织为图模式或中间结果 CTE，再连接属性与文档。

### 2.3 文档：JSON 字段和嵌套对象

```sql
SELECT work_id AS paperid,
       document->>'abstract' AS abstract,
       document->>'language' AS language,
       document->'authors' AS authors
FROM work_doc
WHERE work_id = 'W123';
```

`->` 保留 JSON 值，`->>` 返回文本；嵌套字段按实际结构访问。文档可能包含可选字段或数组。摘要若以倒排位置字典存储，应按位置重建，或使用数据库提供的转换函数，不能将 JSON 串当作连续正文。

### 2.4 向量：语义 Top-k 与种子相似查询

自然语言查询先由与语料一致的 embedding 模型生成向量，再将向量作为参数交给数据库。例如使用余弦距离算子：

```sql
SELECT work_id AS paperid,
       embedding <=> %(embedding)s::vector AS distance,
       1 - (embedding <=> %(embedding)s::vector) AS score
FROM work_vec
ORDER BY embedding <=> %(embedding)s::vector
LIMIT %(limit)s;
```

这里的 `%(embedding)s` 和 `%(limit)s` 是服务端 SQL 模板绑定参数，并非用户输入的 SQL 字面量。通用查询接口只接收 SQL 字符串时，使用该接口支持的实际值；自然语言向量生成及绑定由 `search_papers_by_natural_language` 封装。

种子论文相似查询从同一个数据库读取其已有向量，排除种子自身，再返回近邻。内置封装为 `find_similar_papers_by_paperid`。分数解释以声明的度量为准；余弦距离越小越近，`1-distance` 越大越近，不将距离误写成相似概率。

## 3. 跨模型组合模式

| 需求 | 组合方式 |
| --- | --- |
| 主题相关工作及其内容 | 向量 Top-k → 关系过滤年份 → 文档摘要 |
| 作者研究进展 | 关系定位作者 → 署名图匹配论文 → 年份聚合及摘要 |
| 某方向的合作机构 | 主题候选 → 署名/机构映射 → 按机构对聚合 |
| 引用扩展与内容比较 | 种子 → 引用图 → 关系属性与文档 |
| 代表作的团队介绍 | 论文集合 → 作者图与机构属性 → 保留共同署名依据 |

确定候选集合后，通过统一标识连接各模型表示。模型操作、连接、排序和聚合由数据库完成；应用保留查询条件、实际结果及分析来源。

## 4. 组合查询示例

### 4.1 语义候选与关系、文档联合

```sql
WITH candidates AS (
    SELECT work_id,
           embedding <=> %(embedding)s::vector AS distance
    FROM work_vec
    ORDER BY embedding <=> %(embedding)s::vector
    LIMIT %(limit)s
)
SELECT w.work_id AS paperid, w.title, w.publication_year AS year,
       d.document->>'abstract' AS abstract,
       c.distance, 1 - c.distance AS score
FROM candidates c
JOIN work w ON w.work_id = c.work_id
LEFT JOIN work_doc d ON d.work_id = w.work_id
WHERE w.publication_year BETWEEN 2015 AND 2024
ORDER BY c.distance;
```

该例先召回再按年份过滤，最终可能少于 k 条；它不等同于“在限定年份全集中取 Top-k”。如任务要求后者，应将过滤放入候选查询中，再排序与取 Top-k。相同区别也适用于机构、venue 或主题约束。

### 4.2 图关系与论文元数据联合

```sql
WITH authored AS (
    SELECT DISTINCT p.properties->>'work_id' AS work_id,
                    a.properties->>'author_id' AS author_id
    FROM scholarly_graph
    MATCH {(p: work_v)-[e: work_author_e]->(a: author_v)}
    WHERE a.properties->>'author_id' = 'A123'
)
SELECT w.work_id AS paperid, w.title, w.publication_year AS year,
       x.author_id AS authorid, d.document->>'abstract' AS abstract
FROM authored x
JOIN work w ON w.work_id = x.work_id
LEFT JOIN work_doc d ON d.work_id = w.work_id
WHERE w.publication_year BETWEEN 2015 AND 2024
ORDER BY w.publication_year, w.work_id;
```

这类结果可再按年份聚合，或结合主题语义候选检查该作者在目标方向上的持续工作。

## 5. 查询检查与错误修正

执行前检查对象与字段是否来自当前模式、ID 映射是否一致、边方向是否正确、JSON 路径和类型是否匹配、向量模型及维度是否一致，以及去重单位和范围是否符合问题。

| 返回情况 | 检查与修正 |
| --- | --- |
| 对象/列不存在 | 重新核对 MCP 模式与别名，将示例对象替换为实际对象 |
| 类型不匹配 | 核对文本日期、整数年份、JSON 文本和业务 ID，按真实类型转换 |
| 图结果不符预期 | 检查图对象、标签、边方向和内部 ID 与业务 ID 的映射 |
| 向量维度或模型不匹配 | 核对查询 embedding 与语料配置；重新使用一致的模型 |
| 数量重复 | 检查署名、主题及文档数组展开造成的多行连接，按目标 ID 去重 |
| 结果为空 | 分步核查实体定位和过滤范围，区别空结果与执行失败 |
| 执行超时 | 根据已有模式及执行信息检查过滤和连接，必要时分解查询；不得把失败当作零结果 |

修正后重新执行，将失败尝试保留在研究过程记录中。返回实体 ID、真实端点、字段、统计口径、实际分数与来源，使结果可被报告和证据共同引用。最终只选择成功且支持当前任务的观察。
