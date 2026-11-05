"""Task-scoped observation use cases; independent from Agent execution commands."""


class ExecutionQueryService:
    def __init__(self, store, auth):
        self.repository = store.execution_queries
        self.auth = auth

    def _scope(self, principal, project_key):
        allowed = self.auth.allowed_resources(principal, project_key, "task:read")
        owner = None if principal.has_scope("admin") else f"service:{principal.identity_id}"
        return {"allowed_keys": allowed, "owner": owner}

    def agent_executions(self, principal, project_key, task_id, *, attempt_id=None, limit=100, cursor=None):
        return self.repository.agent_executions(
            project_key, task_id, **self._scope(principal, project_key),
            attempt_id=attempt_id, limit=limit, cursor=cursor,
        )

    def agent_execution(self, principal, project_key, task_id, execution_id):
        return self.repository.agent_execution(
            project_key, task_id, execution_id, **self._scope(principal, project_key),
        )

    def agent_steps(self, principal, project_key, task_id, execution_id, *, limit=100, cursor=None):
        return self.repository.agent_steps(
            project_key, task_id, execution_id, **self._scope(principal, project_key), limit=limit, cursor=cursor,
        )

    def agent_step(self, principal, project_key, task_id, execution_id, step_id):
        return self.repository.agent_step(
            project_key, task_id, execution_id, step_id, **self._scope(principal, project_key),
        )

    def tree(self, principal, project_key, task_id, attempt_id, *, limit=100, cursor=None):
        return self.repository.tree(
            project_key, task_id, attempt_id, **self._scope(principal, project_key), limit=limit, cursor=cursor,
        )
