-- Identical knowledge content can be independently registered by multiple runs.
ALTER TABLE testpilot.knowledge_evidence DROP CONSTRAINT knowledge_evidence_pkey;
ALTER TABLE testpilot.knowledge_evidence ADD PRIMARY KEY (tenant_id,project_id,run_id,evidence_id);
