-- M6.08 评估发现 2 修复：知识库块元数据补充公司代码，支撑检索侧按公司过滤。
-- 背景：多公司年报共库检索（Milvus fin_kb）未按公司过滤，审计机构类问题
-- 跨公司污染（宁德问答命中平安的审计段落）。Milvus fin_kb 同步增加
-- company_code 字段（scripts/_milvus_schema.py），search_kb 检索时
-- 以对话绑定报表的公司代码做表达式过滤。

ALTER TABLE kb_chunks
  ADD COLUMN company_code VARCHAR(16) NOT NULL DEFAULT '' COMMENT '公司代码（6 位 A 股代码；过滤检索用）' AFTER doc_id;

CREATE INDEX idx_kb_chunks_company_code ON kb_chunks (company_code);
