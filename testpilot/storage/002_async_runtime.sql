ALTER TABLE testpilot.tasks DROP CONSTRAINT tasks_state_check;
ALTER TABLE testpilot.tasks ADD CONSTRAINT tasks_state_check CHECK (state IN
 ('QUEUED','RUNNING','WAITING_EXTERNAL','RECONCILING','CANCELLING','COMPLETED','NEEDS_REVIEW','CANCELLED'));
ALTER TABLE testpilot.tasks ADD COLUMN next_wakeup_at timestamptz;
ALTER TABLE testpilot.tasks ADD COLUMN progress_json jsonb NOT NULL DEFAULT '{}';
ALTER TABLE testpilot.operations DROP CONSTRAINT operations_state_check;
ALTER TABLE testpilot.operations ADD CONSTRAINT operations_state_check CHECK (state IN
 ('PREPARED','DISPATCHING','PENDING','UNKNOWN','SUCCEEDED','CANCELLED'));
ALTER TABLE testpilot.operations ADD COLUMN dispatched_at timestamptz;
ALTER TABLE testpilot.operations ADD UNIQUE (tenant_id,project_id,id,task_id,run_id);
CREATE TABLE testpilot.external_jobs (
 tenant_id text NOT NULL, project_id text NOT NULL, operation_id text NOT NULL,
 task_id text NOT NULL, run_id text NOT NULL, external_id text NOT NULL,
 state text NOT NULL CHECK (state IN ('PENDING','SUCCEEDED','CANCELLED','UNKNOWN')),
 poll_count integer NOT NULL DEFAULT 0 CHECK (poll_count >= 0),
 deadline_at timestamptz NOT NULL, next_check_at timestamptz NOT NULL,
 PRIMARY KEY (tenant_id,project_id,operation_id),
 FOREIGN KEY (tenant_id,project_id,operation_id,task_id,run_id) REFERENCES testpilot.operations(tenant_id,project_id,id,task_id,run_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE INDEX external_wait ON testpilot.tasks(state,next_wakeup_at,lease_until);
