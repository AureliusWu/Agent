from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapters import (
    CallableModelProvider,
    CompletionCallable,
    DatabaseTraceExporter,
    DeclarativeExtensionProvider,
    DefaultContextProvider,
    DefaultEvaluator,
    DefaultMemoryProvider,
    DefaultPermissionPolicy,
    DefaultVerifier,
    DefaultWorkspaceProvider,
    RuntimeToolProvider,
    SqliteTaskStore,
    ToolCallable,
)
from .contracts import ContextProvider, Evaluator, ExtensionProvider, MemoryProvider, ModelProvider, PermissionPolicy, TaskStore, ToolProvider, TraceExporter, Verifier, WorkspaceProvider
from .errors import KernelContractError


KERNEL_CONTRACT_VERSION = "1.0"


@dataclass(frozen=True)
class KernelServices:
    model: ModelProvider
    tools: ToolProvider
    permissions: PermissionPolicy
    context: ContextProvider
    memory: MemoryProvider
    workspace: WorkspaceProvider
    tasks: TaskStore
    evaluator: Evaluator
    verifier: Verifier
    trace: TraceExporter
    extensions: ExtensionProvider


SERVICE_CONTRACTS: dict[str, type[Any]] = {
    "model": ModelProvider,
    "tools": ToolProvider,
    "permissions": PermissionPolicy,
    "context": ContextProvider,
    "memory": MemoryProvider,
    "workspace": WorkspaceProvider,
    "tasks": TaskStore,
    "evaluator": Evaluator,
    "verifier": Verifier,
    "trace": TraceExporter,
    "extensions": ExtensionProvider,
}


def validate_kernel_services(services: KernelServices) -> KernelServices:
    invalid = [name for name, contract in SERVICE_CONTRACTS.items() if not isinstance(getattr(services, name, None), contract)]
    if invalid:
        raise KernelContractError("内核服务不满足稳定接口", details={"invalid_services": invalid})
    return services


def build_kernel_services(completion_fn: CompletionCallable | None = None, tool_execute_fn: ToolCallable | None = None) -> KernelServices:
    if completion_fn is None:
        from ..provider import completion

        completion_fn = completion
    permission_policy = DefaultPermissionPolicy()
    return validate_kernel_services(
        KernelServices(
            model=CallableModelProvider(completion_fn),
            tools=RuntimeToolProvider(tool_execute_fn, permission_policy) if tool_execute_fn else RuntimeToolProvider(permission_policy=permission_policy),
            permissions=permission_policy,
            context=DefaultContextProvider(),
            memory=DefaultMemoryProvider(),
            workspace=DefaultWorkspaceProvider(),
            tasks=SqliteTaskStore(),
            evaluator=DefaultEvaluator(),
            verifier=DefaultVerifier(),
            trace=DatabaseTraceExporter(),
            extensions=DeclarativeExtensionProvider(),
        )
    )


def kernel_manifest(services: KernelServices | None = None) -> dict[str, Any]:
    active = validate_kernel_services(services or build_kernel_services())
    return {
        "contract_version": KERNEL_CONTRACT_VERSION,
        "services": {name: type(getattr(active, name)).__name__ for name in SERVICE_CONTRACTS},
        "composition": "trusted_internal",
        "extension_replaceable": False,
    }
