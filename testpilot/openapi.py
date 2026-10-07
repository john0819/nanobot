"""Versioned local API contract; identity never appears in model task input."""


def document(durable: bool = False) -> dict[str, object]:
    from testpilot.governance import ApprovalDecision
    from testpilot.task_api import TaskRequest

    json_task: dict[str, object] = {"application/json": {"schema": {"$ref": "#/components/schemas/TaskSnapshot"}}}
    error = {"description": "Request rejected", "content": {"text/plain": {"schema": {"type": "string"}}}}
    task_id = {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}

    def response(description: str, content: dict[str, object] = json_task) -> dict[str, object]:
        return {"description": description, "content": content}

    return {
        "openapi": "3.1.0", "info": {"title": "TestPilot local Task API", "version": "0.5.0" if durable else "0.2.0",
                                     "description": "PG asynchronous Job scheduler, persistent progress guard and authenticated SSE replay." if durable else "Loopback development only. In-memory task state; no durable recovery."},
        "servers": [{"url": "http://127.0.0.1:8920"}],
        "security": [{"LocalBearer": []}],
        "paths": {
            **({
                "/v1/tasks/{task_id}/approval": {
                    "get": {"parameters": [task_id], "responses": {"200": response("Canonical execution request; owner or project reviewer"), "401": error, "403": error, "404": error}},
                    "post": {"parameters": [task_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApprovalDecision"}}}},
                             "responses": {"200": response("Independent decision recorded"), "401": error, "403": error, "409": error, "422": error}},
                },
                "/v1/tasks/{task_id}/plan": {"get": {"parameters": [task_id], "responses": {"200": response("Persisted bounded plan version"), "401": error, "404": error}}},
                "/health/ready": {"get": {"security": [], "responses": {"200": {"description": "PG schema ready"}, "503": error}}},
                "/v1/tasks/{task_id}/events": {"get": {
                    "parameters": [task_id, {"name": "after", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807}}],
                    "responses": {"200": response("Scoped durable events ordered by sequence", {"application/json": {"schema": {"type": "object", "required": ["events"]}}}), "401": error, "404": error, "422": error, "503": error},
                }},
                "/v1/tasks/{task_id}/events/stream": {"get": {
                    "parameters": [task_id,
                                   {"name": "Last-Event-ID", "in": "header", "schema": {"type": "integer", "minimum": 0}},
                                   {"name": "after", "in": "query", "schema": {"type": "integer", "minimum": 0}}],
                    "responses": {
                        "200": response("Authorized durable SSE; disconnect does not cancel task", {"text/event-stream": {"schema": {"type": "string"}}}),
                        "401": error, "404": error, "409": {"description": "Cursor requires resync", "content": {"application/json": {"schema": {"type": "object", "required": ["resync_required", "snapshot"]}}}},
                        "422": error, "503": error,
                    },
                }},
            } if durable else {}),
            "/health/live": {"get": {"security": [], "responses": {"200": {"description": "Process healthy"}}}},
            "/openapi.json": {"get": {"responses": {"200": {"description": "OpenAPI contract"}, "401": error}}},
            "/v1/tasks": {"post": {
                "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
                                "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,100}$"}}],
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/TaskRequest"}}}},
                "responses": {"202": response("Admitted asynchronous task"), "200": response("Idempotent replay"),
                              "401": error, "409": error, "422": error, "429": error, "413": error},
            }, **({"get": {"parameters": [{"name": "review", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": response("Owner task list or role-gated pending review queue"), "401": error, "403": error}}} if durable else {})},
            "/v1/tasks/{task_id}": {"get": {"parameters": [task_id], "responses": {
                "200": response("Scoped task snapshot"), "401": error, "404": error}}},
            "/v1/tasks/{task_id}/report": {"get": {"parameters": [task_id], "responses": {
                "200": response("Validated or safe NEEDS_REVIEW report", {"application/json": {
                    "schema": {"type": "object", "required": ["task_id", "report_validated", "quality_verdict", "validation_gaps"]}}}),
                "401": error, "404": error, "409": error}}},
            "/v1/tasks/{task_id}/cancel": {"post": {"parameters": [task_id], "responses": {
                "200": response("Cancellation resolved or terminal state unchanged"),
                **({"202": response("Cancellation intent persisted; query until cleanup is confirmed")} if durable else {}),
                "401": error, "404": error}}},
            "/v1/tasks/{task_id}/artifacts/{hash}": {"get": {
                "parameters": [task_id, {"name": "hash", "in": "path", "required": True,
                                        "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}],
                "responses": {"200": response("Integrity checked scoped artifact", {"application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}}}), "401": error, "404": error, "409": error},
            }},
        },
        "components": {
            "securitySchemes": {"LocalBearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "TaskRequest": TaskRequest.model_json_schema(),
                **({"ApprovalDecision": ApprovalDecision.model_json_schema()} if durable else {}),
                "TaskSnapshot": {"type": "object", "required": ["task_id", "state", "mode", "created_at", "error", "report_ready", "operation_id"] + (["run_id", "lease_epoch", "model_rounds"] if durable else []),
                                 "properties": {
                                     **({"run_id": {"type": "string"}, "lease_epoch": {"type": "integer", "minimum": 0},
                                         "model_rounds": {"type": "integer", "minimum": 0, "maximum": 40},
                                         "waiting_reason": {"type": ["string", "null"]}, "next_wakeup_at": {"type": ["string", "null"], "format": "date-time"}} if durable else {}),
                                     "task_id": {"type": "string"},
                                     "state": {"enum": ["QUEUED", "WAITING_APPROVAL", "RUNNING", "WAITING_EXTERNAL", "RECONCILING", "CANCELLING", "COMPLETED", "NEEDS_REVIEW", "CANCELLED"]},
                                     "mode": {"enum": ["healthy", "retry-write-bug"]},
                                     "created_at": {"type": "string", "format": "date-time"},
                                     "error": {"type": ["string", "null"]}, "report_ready": {"type": "boolean"},
                                     "operation_id": {"type": ["string", "null"]},
                                 }},
            },
        },
    }
