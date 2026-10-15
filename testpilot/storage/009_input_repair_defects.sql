ALTER TABLE testpilot.tasks ADD COLUMN input_request jsonb;
ALTER TABLE testpilot.task_runs ADD COLUMN report_repairs integer NOT NULL DEFAULT 0 CHECK (report_repairs BETWEEN 0 AND 1);
CREATE TABLE testpilot.defect_drafts (
 tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL,
 task_id text NOT NULL, run_id text NOT NULL, case_id text NOT NULL,
 creator_id text NOT NULL, content jsonb NOT NULL, content_hash text NOT NULL,
 status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','APPROVED','DENIED')),
 reviewer_id text, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), decided_at timestamptz,
 PRIMARY KEY (tenant_id,project_id,id), UNIQUE (tenant_id,project_id,run_id,case_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE TABLE testpilot.defect_events (
 tenant_id text NOT NULL, project_id text NOT NULL, defect_id text NOT NULL,
 version integer NOT NULL, actor_id text NOT NULL, type text NOT NULL, content_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,defect_id,version),
 FOREIGN KEY (tenant_id,project_id,defect_id) REFERENCES testpilot.defect_drafts(tenant_id,project_id,id)
);
