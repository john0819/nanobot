CREATE TABLE testpilot.artifact_catalog (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'), kind text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), deleted_at timestamptz,
 PRIMARY KEY (tenant_id,project_id,run_id,hash),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
INSERT INTO testpilot.artifact_catalog (tenant_id,project_id,task_id,run_id,hash,kind)
 SELECT tenant_id,project_id,task_id,run_id,result->>'junit_hash','JUNIT' FROM testpilot.operations WHERE result->>'junit_hash' ~ '^[0-9a-f]{64}$' ON CONFLICT DO NOTHING;
INSERT INTO testpilot.artifact_catalog (tenant_id,project_id,task_id,run_id,hash,kind)
 SELECT tenant_id,project_id,task_id,run_id,result->>'log_hash','PRIVATE_LOG' FROM testpilot.operations WHERE result->>'log_hash' ~ '^[0-9a-f]{64}$' ON CONFLICT DO NOTHING;
INSERT INTO testpilot.artifact_catalog (tenant_id,project_id,task_id,run_id,hash,kind)
 SELECT tenant_id,project_id,task_id,run_id,body->>'transcript_ref','PRIVATE_TRANSCRIPT' FROM testpilot.checkpoints WHERE body->>'transcript_ref' ~ '^[0-9a-f]{64}$' ON CONFLICT DO NOTHING;
INSERT INTO testpilot.artifact_catalog (tenant_id,project_id,task_id,run_id,hash,kind)
 SELECT tenant_id,project_id,task_id,run_id,body->>'artifact_hash','PRIVATE_KNOWLEDGE' FROM testpilot.knowledge_evidence WHERE body->>'artifact_hash' ~ '^[0-9a-f]{64}$' ON CONFLICT DO NOTHING;
CREATE TABLE testpilot.trace_records (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind text NOT NULL, duration_ms double precision NOT NULL,
 input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0,
 attributes jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE INDEX trace_task ON testpilot.trace_records(tenant_id,project_id,task_id,id);
