"""Worker interceptor: an older worker refuses a session state written by newer code (goodqa#16096 deploy part 2).

The check runs when a workflow run starts or is replayed, before any workflow code of the older version touches the state.
It raises engine.deploy_guard.StateTooNew, a plain RuntimeError, which Temporal treats as a WORKFLOW TASK failure: the run is
not failed, its history is untouched, the task is retried, and the session resumes the moment a worker that supports the
state's version polls the queue (roll forward). A failed workflow (ApplicationError) would end the session and is NOT used."""
from temporalio.worker import ExecuteWorkflowInput, Interceptor, WorkflowInboundInterceptor, WorkflowInterceptorClassInput

from .deploy_guard import check_state


class StateGuardInterceptor(Interceptor):
    def __init__(self, supported_ver: int):
        self.supported_ver = supported_ver

    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput):
        supported = self.supported_ver

        class _Guard(WorkflowInboundInterceptor):
            async def execute_workflow(self, input: ExecuteWorkflowInput):
                check_state(input.args[0] if input.args else None, supported)
                return await super().execute_workflow(input)
        return _Guard
