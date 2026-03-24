"""Service runner — executes automated pipeline steps via the ServiceRegistry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

if TYPE_CHECKING:
    from swarm.pipelines.engine import PipelineEngine
    from swarm.pipelines.models import Pipeline, PipelineStep
    from swarm.services.registry import ServiceRegistry

_log = get_logger("services.runner")


class ServiceRunner:
    """Runs automated pipeline steps through the ServiceRegistry.

    When a pipeline step with ``step_type=automated`` becomes READY,
    the runner invokes the matching service and reports the result
    back to the PipelineEngine.
    """

    def __init__(
        self,
        registry: ServiceRegistry,
        engine: PipelineEngine,
    ) -> None:
        self._registry = registry
        self._engine = engine

    async def run_step(
        self,
        pipeline: Pipeline,
        step: PipelineStep,
    ) -> ServiceResult:
        """Execute an automated step's service and update the pipeline."""
        if not step.service:
            result = ServiceResult(success=False, error="no service specified")
            self._engine.fail_step(pipeline.id, step.id, error=result.error)
            return result

        if not self._registry.has(step.service):
            result = ServiceResult(
                success=False,
                error=f"service not registered: {step.service}",
            )
            self._engine.fail_step(pipeline.id, step.id, error=result.error)
            return result

        context = ServiceContext(
            pipeline_id=pipeline.id,
            step_id=step.id,
            pipeline_name=pipeline.name,
            step_name=step.name,
        )

        step.start()
        result = await self._registry.execute(step.service, step.config, context)

        if result.success:
            self._engine.complete_step(pipeline.id, step.id, result=result.data)
            _log.info(
                "automated step %s/%s completed via service %s",
                pipeline.id,
                step.id,
                step.service,
            )
        else:
            self._engine.fail_step(pipeline.id, step.id, error=result.error)
            _log.warning(
                "automated step %s/%s failed: %s",
                pipeline.id,
                step.id,
                result.error,
            )

        return result
