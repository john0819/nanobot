CREATE SCHEMA IF NOT EXISTS testpilot;
CREATE TABLE testpilot.tasks (
 tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL,
 actor_id text NOT NULL, mode text NOT NULL CHECK (mode IN ('healthy','retry-write-bug')),
 state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED','RUNNING','CANCELLING','COMPLETED','NEEDS_REVIEW','CANCELLED')),
 run_id text NOT NULL, target jsonb NOT NULL, report jsonb, error text,
 lease_owner text, lease_epoch bigint NOT NULL DEFAULT 0, lease_until timestamptz,
 model_rounds integer NOT NULL DEFAULT 0 CHECK (model_rounds BETWEEN 0 AND 4),
 cancel_requested boolean NOT NULL DEFAULT false,
 next_event_seq bigint NOT NULL DEFAULT 0, state_version bigint NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,id), UNIQUE (tenant_id,project_id,id,actor_id)
);
CREATE INDEX runnable ON testpilot.tasks(state,lease_until,created_at);
CREATE TABLE testpilot.task_runs (
 tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, task_id text NOT NULL,
 sequence integer NOT NULL DEFAULT 1, target jsonb NOT NULL,
 PRIMARY KEY (tenant_id,project_id,id), UNIQUE (tenant_id,project_id,task_id,sequence),
 UNIQUE (tenant_id,project_id,id,task_id),
 FOREIGN KEY (tenant_id,project_id,task_id) REFERENCES testpilot.tasks(tenant_id,project_id,id)
);
ALTER TABLE testpilot.tasks ADD FOREIGN KEY (tenant_id,project_id,run_id,id)
 REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE testpilot.request_dedup (
 tenant_id text NOT NULL, project_id text NOT NULL, actor_id text NOT NULL,
 request_key text NOT NULL, mode text NOT NULL, task_id text NOT NULL,
 PRIMARY KEY (tenant_id,project_id,actor_id,request_key),
 FOREIGN KEY (tenant_id,project_id,task_id,actor_id) REFERENCES testpilot.tasks(tenant_id,project_id,id,actor_id)
);
CREATE TABLE testpilot.operations (
 tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, task_id text NOT NULL,
 run_id text NOT NULL, intent_ref text NOT NULL DEFAULT 'gateway-suite-v1',
 state text NOT NULL DEFAULT 'PREPARED' CHECK (state IN ('PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED','CANCELLED')),
 result jsonb, external_id text, lease_epoch bigint NOT NULL,
 PRIMARY KEY (tenant_id,project_id,id), UNIQUE (tenant_id,project_id,run_id,intent_ref),
 FOREIGN KEY (tenant_id,project_id,task_id) REFERENCES testpilot.tasks(tenant_id,project_id,id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE TABLE testpilot.attempts (
 tenant_id text NOT NULL, project_id text NOT NULL, operation_id text NOT NULL,
 sequence integer NOT NULL, dispatch_certainty text NOT NULL CHECK (dispatch_certainty IN ('UNKNOWN','SENT')),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(), ended_at timestamptz,
 PRIMARY KEY (tenant_id,project_id,operation_id,sequence),
 FOREIGN KEY (tenant_id,project_id,operation_id) REFERENCES testpilot.operations(tenant_id,project_id,id)
);
CREATE TABLE testpilot.checkpoints (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 event_seq bigint NOT NULL, schema_version integer NOT NULL DEFAULT 1,
 body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,task_id,event_seq),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE TABLE testpilot.task_events (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, run_id text NOT NULL,
 event_seq bigint NOT NULL, type text NOT NULL, public_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,project_id,task_id,event_seq),
 FOREIGN KEY (tenant_id,project_id,task_id) REFERENCES testpilot.tasks(tenant_id,project_id,id),
 FOREIGN KEY (tenant_id,project_id,run_id,task_id) REFERENCES testpilot.task_runs(tenant_id,project_id,id,task_id)
);
CREATE TABLE testpilot.outbox (
 tenant_id text NOT NULL, project_id text NOT NULL, task_id text NOT NULL, event_seq bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), processed_at timestamptz,
 PRIMARY KEY (tenant_id,project_id,task_id,event_seq),
 FOREIGN KEY (tenant_id,project_id,task_id,event_seq) REFERENCES testpilot.task_events(tenant_id,project_id,task_id,event_seq)
);
