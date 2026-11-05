# Long-running Operations executed by an in-process worker

Every mutating API call creates a persisted Operation (pending → running → succeeded/failed) that an asyncio worker inside the FastAPI process executes; clients poll it. We deliberately skip Celery/Redis/Temporal to keep moving parts minimal for a demo. Consequence: Operations interrupted by a Control Plane restart are marked failed on startup rather than resumed; a production system would use a durable workflow engine (e.g. Temporal).
