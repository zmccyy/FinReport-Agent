-- M5.07 知识库块元数据表 — spec §3.4 [MySQL: kb_chunks (chunk_id, doc_id, text, page, position)]
-- 由 scripts/build_kb.py 写入（脚本环境直连，不受 L3 只读约束）。

CREATE TABLE IF NOT EXISTS kb_chunks (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  doc_id      BIGINT      NOT NULL COMMENT '关联 report.id；未入库 sample 用文件名派生稳定 id',
  chunk_id    VARCHAR(64) NOT NULL COMMENT '唯一块标识 doc{doc_id}-p{page}-n{position}',
  chunk_type  VARCHAR(16) NOT NULL DEFAULT 'TEXT' COMMENT 'TEXT / TABLE_HEADER / TABLE_ROW',
  text        MEDIUMTEXT  NOT NULL COMMENT '块原文（用于排查与展示）',
  page        INT         NOT NULL COMMENT '页码（1 起）',
  position    INT         NOT NULL COMMENT '页内块序号（1 起）',
  created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_kb_chunks_chunk_id (chunk_id),
  INDEX idx_kb_chunks_doc_id (doc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识库块元数据（spec §3.4）';
