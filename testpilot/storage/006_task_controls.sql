ALTER TABLE testpilot.tasks DROP CONSTRAINT tasks_state_check;
ALTER TABLE testpilot.tasks ADD CONSTRAINT tasks_state_check CHECK (state IN
 ('QUEUED','WAITING_APPROVAL','RUNNING','WAITING_EXTERNAL','RECONCILING','PAUSED','CANCELLING','COMPLETED','NEEDS_REVIEW','CANCELLED'));
ALTER TABLE testpilot.tasks ADD COLUMN paused_from text;
CREATE TABLE testpilot.task_inputs (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 client_request_id text NOT NULL, actor_id text NOT NULL, kind text NOT NULL CHECK (kind='NOTE'),
 text text NOT NULL, payload_hash text NOT NULL, consumed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,task_id,client_request_id),
 FOREIGN KEY (tenant_id,project_id,task_id,actor_id) REFERENCES testpilot.tasks(tenant_id,project_id,id,actor_id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
