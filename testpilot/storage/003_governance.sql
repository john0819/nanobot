ALTER TABLE testpilot.tasks DROP CONSTRAINT tasks_state_check;
ALTER TABLE testpilot.tasks ADD CONSTRAINT tasks_state_check CHECK (state IN
 ('QUEUED','WAITING_APPROVAL','RUNNING','WAITING_EXTERNAL','RECONCILING','CANCELLING','COMPLETED','NEEDS_REVIEW','CANCELLED'));
ALTER TABLE testpilot.tasks ADD COLUMN goal text NOT NULL DEFAULT 'Validate gateway fixture';
ALTER TABLE testpilot.tasks ADD COLUMN approval_required boolean NOT NULL DEFAULT false;
ALTER TABLE testpilot.tasks ADD COLUMN knowledge_required boolean NOT NULL DEFAULT false;
ALTER TABLE testpilot.tasks ADD COLUMN activated_at timestamptz;
UPDATE testpilot.tasks SET activated_at=created_at;
ALTER TABLE testpilot.tasks ADD COLUMN model_limit integer NOT NULL DEFAULT 4 CHECK (model_limit BETWEEN 4 AND 40);
ALTER TABLE testpilot.tasks DROP CONSTRAINT tasks_model_rounds_check;
ALTER TABLE testpilot.tasks ADD CONSTRAINT tasks_model_rounds_check CHECK (model_rounds>=0 AND model_rounds<=model_limit);
ALTER TABLE testpilot.request_dedup ADD COLUMN payload_hash text NOT NULL DEFAULT '';
CREATE TABLE testpilot.approvals (
 tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL,
 task_id text NOT NULL, run_id text NOT NULL, request_hash text NOT NULL,
 requester_id text NOT NULL, reviewer_id text,
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','DENIED','EXPIRED')),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '15 minutes',
 consumed_operation_id text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,id), UNIQUE (tenant_id,project_id,run_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id),
 FOREIGN KEY (tenant_id,project_id,consumed_operation_id) REFERENCES testpilot.operations(tenant_id,project_id,id)
);
CREATE TABLE testpilot.plans (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 version integer NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,run_id,version),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE TABLE testpilot.knowledge_evidence (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 evidence_id text NOT NULL, query_hash text NOT NULL, body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,evidence_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
