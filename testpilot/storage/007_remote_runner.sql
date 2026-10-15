CREATE TABLE testpilot.runner_jobs (
 tenant_id text NOT NULL, project_id text NOT NULL, operation_id text NOT NULL,
 request_hash text NOT NULL, task_body jsonb NOT NULL, operation_body jsonb NOT NULL,
 state text NOT NULL DEFAULT 'ACCEPTED' CHECK (state IN ('ACCEPTED','DISPATCHING','RUNNING','FINISHED','UNKNOWN','CANCELLED')),
 result jsonb, cancel_requested boolean NOT NULL DEFAULT false,
 lease_epoch bigint NOT NULL DEFAULT 0, lease_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,operation_id),
 FOREIGN KEY (tenant_id,project_id,operation_id) REFERENCES testpilot.operations(tenant_id,project_id,id)
);
