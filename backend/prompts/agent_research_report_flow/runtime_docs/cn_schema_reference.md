# SciSciNetV2 CN Schema

## 1. 关系表

### cn_papers
字段：paperid(text), doi(text), year(bigint), date(text，ISO 日期字符串), doctype(text), cited_by_count(bigint), is_retracted(boolean), reference_count(integer), citation_count(integer), c3(integer), c5(integer), c10(integer), disruption(double precision), atyp_median_z(double precision), atyp_10pct_z(double precision), atyp_pairs(bigint), wsb_mu(double precision), wsb_sigma(double precision), wsb_cinf(double precision), sb_b(double precision), sb_t(bigint), team_size(integer), institution_count(integer), patent_count(integer), newsfeed_count(integer), nct_count(integer), nih_count(integer), nsf_count(integer)

年份过滤优先使用 `year`；不要对 `date(text)` 使用 `EXTRACT`、日期减法或隐式日期运算。只有需要精确月份/日期时才使用经过验证的文本转换。

索引：PRIMARY KEY (paperid)

### cn_paperdetails
字段：paperid(text), doi(text), title(text), display_name(text), year(bigint), date(text), doctype(text), cited_by_count(bigint), is_retracted(boolean), is_paratext(boolean), cited_by_api_url(text), abstract_inverted_index(text), language(text)

索引：PRIMARY KEY (paperid); idx_cn_paperdetails_title_lower ON lower(title); idx_cn_paperdetails_title_fts USING gin(to_tsvector('english', title))

### cn_papertitleabstract
字段：paperid(text), title(text), abstract_inverted_index(jsonb), language(text)

索引：PRIMARY KEY (paperid); idx_cn_papertitleabstract_title ON title; idx_cn_papertitleabstract_title_low ON lower(title)

### cn_authors
字段：authorid(text), avg_c10(double precision), avg_logc10(double precision), productivity(integer), h_index(integer), author_name(text), inference_sources(bigint), inference_counts(bigint), p_gf(double precision)

索引：idx_cn_authors_name ON author_name; idx_cn_authors_name_low ON lower(author_name)

### cn_author_details
字段：authorid(text), orcid(text), display_name(text), display_name_alternatives(text), works_count(bigint), cited_by_count(bigint), last_known_institution(text), works_api_url(text), updated_date(text)

索引：PRIMARY KEY (authorid); idx_cn_author_details_name ON display_name; idx_cn_author_details_name_low ON lower(display_name)

### cn_affiliations
字段：institution_id(text), affiliation_name(text), ror(text), homepage_url(text), country_code(text), type(text), h_index(double precision), productivity(double precision), avg_c10(double precision), avg_logc10(double precision)

索引：PRIMARY KEY (institution_id); idx_cn_affiliation_name ON affiliation_name; idx_cn_affiliation_name_low ON lower(affiliation_name)

### cn_fields
字段：id(text), wikidata(text), display_name(text), level(bigint), description(text), works_count(bigint), cited_by_count(bigint), image_url(text), image_thumbnail_url(text), works_api_url(text), updated_date(text), fieldid(text), field_id(bigint)

索引：PRIMARY KEY (id); idx_cn_fields_field_id ON field_id

### cn_paperfields
字段：paperid(text), fieldid(text), score_openalex(double precision)

索引：idx_cn_paperfields_paperid ON paperid; idx_cn_paperfields_fieldid ON fieldid; idx_cn_paperfields_field_score_paper ON (fieldid, score_openalex, paperid); idx_cn_paperfields_paper_field_score ON (paperid, fieldid, score_openalex)

### cn_paper_author_affiliation
字段：paperid(text), author_position(text), authorid(text), institutionid(text), raw_affiliation_string(text)

索引：idx_cn_paperauthoraffiliation_paperid ON paperid; idx_cn_paperauthoraffiliation_authorid ON authorid; idx_cn_paperauthoraffiliation_affiliation ON institutionid; idx_cn_paperauthoraffiliation_institution_paper ON (institutionid, paperid); idx_cn_paperauthoraffiliation_author_paper ON (authorid, paperid)

### cn_paper_ref
字段：citing_paperid(text), cited_paperid(text), year(integer), ref_year(integer), year_diff(integer)

索引：无

### cn_paper_refs
字段：citing_paperid(bigint), cited_paperid(bigint), year(integer), ref_year(integer), year_diff(integer)

索引：idx_citing ON citing_paperid; idx_cited ON cited_paperid; idx_refs_cited_citing ON (cited_paperid, citing_paperid)

## 2. 图顶点表

### cn_paper_nodes
字段：id(bigint), properties(jsonb)

索引：PRIMARY KEY (id)

说明：通常 `id` 对应 paperid 去掉 `W` 前缀后的 bigint。

### cn_author_nodes
字段：id(bigint), properties(jsonb)

索引：PRIMARY KEY (id)

### cn_affiliation_nodes
字段：id(bigint), properties(jsonb)

索引：PRIMARY KEY (id)

### cn_field_nodes
字段：id(bigint), properties(jsonb)

索引：PRIMARY KEY (id)

### cn_paper_author_affiliation_nodes
字段：id(bigint), properties(jsonb)

索引：PRIMARY KEY (id); idx_cn_paa_nodes_institutionid_prop ON ((properties->>'institutionid'))

说明：这是 paper-author-affiliation 三元事实节点，不是真实实体节点。常用属性包括 paperid、authorid、institutionid、author_position 等，具体以 properties 内容为准。

## 3. 图边表

边表通用字段：startid(bigint), startlabelid(oid), endid(bigint), endlabelid(oid), properties(jsonb)

### cn_writen_by
连接：cn_paper_nodes -> cn_paper_author_affiliation_nodes

索引：无普通 btree 索引；用于 `cn_writen_by_network` 图加载后的邻接扩展。

### cn_authored_in
连接：cn_author_nodes -> cn_paper_author_affiliation_nodes

索引：无普通 btree 索引；用于 `cn_authored_in_network` 图加载后的邻接扩展。

### cn_affiliated_with
连接：cn_affiliation_nodes -> cn_paper_author_affiliation_nodes

索引：无普通 btree 索引；用于 `cn_affiliated_with_network` 图加载后的邻接扩展。

### cn_in_field
连接：cn_paper_nodes -> cn_field_nodes

索引：无普通 btree 索引；用于 `cn_in_field_network` 图加载后的邻接扩展。

## 4. 图对象

图对象表字段：id(integer), labelname(text)

### cn_writen_by_network
组成：ELABEL=[cn_writen_by], VLABEL=[cn_paper_nodes, cn_paper_author_affiliation_nodes]

常用 MATCH：

```sql
FROM cn_writen_by_network
MATCH {(p: cn_paper_nodes)-[e: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}
```

### cn_authored_in_network
组成：ELABEL=[cn_authored_in], VLABEL=[cn_author_nodes, cn_paper_author_affiliation_nodes]

常用 MATCH：

```sql
FROM cn_authored_in_network
MATCH {(a: cn_author_nodes)-[e: cn_authored_in]->(paa: cn_paper_author_affiliation_nodes)}
```

### cn_affiliated_with_network
组成：ELABEL=[cn_affiliated_with], VLABEL=[cn_affiliation_nodes, cn_paper_author_affiliation_nodes]

常用 MATCH：

```sql
FROM cn_affiliated_with_network
MATCH {(i: cn_affiliation_nodes)-[e: cn_affiliated_with]->(paa: cn_paper_author_affiliation_nodes)}
```

### cn_in_field_network
组成：ELABEL=[cn_in_field], VLABEL=[cn_paper_nodes, cn_field_nodes]

常用 MATCH：

```sql
FROM cn_in_field_network
MATCH {(p: cn_paper_nodes)-[e: cn_in_field]->(f: cn_field_nodes)}
```

## 关于领域的一些数据字段说明
  - cn_fields.fieldid 是领域 ID，例如 Computer science = C41008148
  - cn_fields.display_name 是领域名称，例如 Computer science
  - cn_fields.level 只有 0 和 1：0 是一级领域，1 是二级概念/子领域
  - 论文和领域通过 cn_paperfields(paperid, fieldid, score_openalex) 关联
  - 查询计算机科学论文时，优先使用 fieldid = 'C41008148'

  计算机相关白名单可以写成：

  Computer science 一级领域：
  C41008148 = Computer science

  常用计算机相关 level=1 子领域：
  C11413529 = Algorithm
  C154945302 = Artificial intelligence
  C118524514 = Computer architecture
  C113775141 = Computer engineering
  C121684516 = Computer graphics (images)
  C9390403 = Computer hardware
  C31258907 = Computer network
  C38652104 = Computer security
  C31972630 = Computer vision
  C124101348 = Data mining
  C2522767166 = Data science
  C77088390 = Database
  C107457646 = Human–computer interaction
  C23123220 = Information retrieval
  C119857082 = Machine learning
  C204321447 = Natural language processing
  C199360897 = Programming language
  C115903868 = Software engineering
C80444323 = Theoretical computer science

## Venue/source 查询

CN 库的 source/venue 关系通过以下对象提供：

- `cn_sources`：原始 source 名称、出版社、ISSN 等；
- `cn_paper_sources`：论文与原始 source 的关联；
- `cn_venue_aliases`：经人工维护的规范 `venue_key` 别名；
- `cn_paper_venue_lookup`：推荐查询视图，已经展开 paper、source 和 venue 别名。

查询 SIGMOD、PVLDB、VLDBJ、ICDE、PACMMOD 等 venue 时，优先使用
`search_papers_by_venue` 或 `aggregate_papers_by_venue`；查询单篇论文的 source/venue
时使用 `get_paper_venue`。不要自行对原始
`source_name` 做模糊匹配。默认年份口径是 `cn_papers.date` 的出版年份；
统计论文数量必须按 `COUNT(DISTINCT paperid)`，避免作者关联导致重复计数。

这些 venue_key 默认相互独立：SIGMOD 不自动包含 PACMMOD，VLDB conference
不自动包含 PVLDB 或 VLDBJ。用户明确要求合并时，才传入多个 venue_key。
