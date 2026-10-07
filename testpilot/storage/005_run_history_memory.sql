CREATE TABLE testpilot.run_results (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 state text NOT NULL CHECK (state IN ('COMPLETED','NEEDS_REVIEW','CANCELLED')),
 report jsonb, error text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,run_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
INSERT INTO testpilot.run_results (tenant_id,project_id,task_id,run_id,state,report,error)
 SELECT tenant_id,project_id,id,run_id,state,report,error FROM testpilot.tasks
 WHERE state IN ('COMPLETED','NEEDS_REVIEW','CANCELLED');
CREATE FUNCTION testpilot.reject_run_result_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'run results are immutable'; END; $$;
CREATE TRIGGER run_results_immutable BEFORE UPDATE ON testpilot.run_results
 FOR EACH ROW EXECUTE FUNCTION testpilot.reject_run_result_update();
CREATE TABLE testpilot.run_requests (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, actor_id text NOT NULL,
 request_key text NOT NULL, payload_hash text NOT NULL, run_id text NOT NULL,
 PRIMARY KEY (tenant_id,project_id,task_id,actor_id,request_key),
 FOREIGN KEY (tenant_id,project_id,task_id,actor_id) REFERENCES testpilot.tasks(tenant_id,project_id,id,actor_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE TABLE testpilot.memory_records (
 tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL,
 source_task_id text NOT NULL, source_run_id text NOT NULL, source_evidence_id text NOT NULL,
 creator_id text NOT NULL, reviewer_id text, user_scope text,
 type text NOT NULL DEFAULT 'CONFIRMED_FAILURE_PATTERN', key text NOT NULL, value jsonb NOT NULL,
 suite_hash text NOT NULL, status text NOT NULL DEFAULT 'CANDIDATE' CHECK (status IN ('CANDIDATE','CONFIRMED','REVOKED','EXPIRED')),
 version integer NOT NULL DEFAULT 1, expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '30 days',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,id), UNIQUE (tenant_id,project_id,source_run_id,key),
 FOREIGN KEY (tenant_id,project_id,source_run_id,source_task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id),
 FOREIGN KEY (tenant_id,project_id,source_run_id) REFERENCES testpilot.run_results(tenant_id,project_id,run_id)
);
CREATE INDEX memory_lookup ON testpilot.memory_records(tenant_id,project_id,suite_hash,status,expires_at);
CREATE TABLE testpilot.memory_events (
 tenant_id text NOT NULL, project_id text NOT NULL, memory_id text NOT NULL, version integer NOT NULL,
 actor_id text NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,memory_id,version),
 FOREIGN KEY (tenant_id,project_id,memory_id) REFERENCES testpilot.memory_records(tenant_id,project_id,id)
);
