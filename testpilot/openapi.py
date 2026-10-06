"""Versioned local API contract; identity never appears in model task input."""


def document() -> dict[str, object]:
    from testpilot.task_api import TaskRequest

    json_task: dict[str, object] = {"application/json": {"schema": {"$ref": "#/components/schemas/TaskSnapshot"}}}
    error = {"description": "Request rejected", "content": {"text/plain": {"schema": {"type": "string"}}}}
    task_id = {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}

    def response(description: str, content: dict[str, object] = json_task) -> dict[str, object]:
        return {"description": description, "content": content}

    return {
        "openapi": "3.1.0", "info": {"title": "TestPilot local Task API", "version": "0.2.0",
                                     "description": "Loopback development only. In-memory task state; no durable recovery."},
        "servers": [{"url": "http://127.0.0.1:8920"}],
        "security": [{"LocalBearer": []}],
        "paths": {
            "/health/live": {"get": {"security": [], "responses": {"200": {"description": "Process healthy"}}}},
            "/openapi.json": {"get": {"responses": {"200": {"description": "OpenAPI contract"}, "401": error}}},
            "/v1/tasks": {"post": {
                "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
                                "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,100}$"}}],
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/TaskRequest"}}}},
                "responses": {"202": response("Admitted asynchronous task"), "200": response("Idempotent replay"),
                              "401": error, "409": error, "422": error, "429": error, "413": error},
            }},
            "/v1/tasks/{task_id}": {"get": {"parameters": [task_id], "responses": {
                "200": response("Scoped task snapshot"), "401": error, "404": error}}},
            "/v1/tasks/{task_id}/report": {"get": {"parameters": [task_id], "responses": {
                "200": response("Validated or safe NEEDS_REVIEW report", {"application/json": {
                    "schema": {"type": "object", "required": ["task_id", "report_validated", "quality_verdict", "validation_gaps"]}}}),
                "401": error, "404": error, "409": error}}},
            "/v1/tasks/{task_id}/cancel": {"post": {"parameters": [task_id], "responses": {
                "200": response("Cancellation resolved or terminal state unchanged"), "401": error, "404": error}}},
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
                "TaskSnapshot": {"type": "object", "required": ["task_id", "state", "mode", "created_at", "error", "report_ready", "operation_id"],
                                 "properties": {
                                     "task_id": {"type": "string"},
                                     "state": {"enum": ["QUEUED", "RUNNING", "COMPLETED", "NEEDS_REVIEW", "CANCELLED"]},
                                     "mode": {"enum": ["healthy", "retry-write-bug"]},
                                     "created_at": {"type": "string", "format": "date-time"},
                                     "error": {"type": ["string", "null"]}, "report_ready": {"type": "boolean"},
                                     "operation_id": {"type": ["string", "null"]},
                                 }},
            },
        },
    }
