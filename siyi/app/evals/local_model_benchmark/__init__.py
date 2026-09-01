"""Repeatable, credential-free local model benchmark v1."""

from importlib import import_module

__all__ = [
    "LocalModelBenchmarkReport",
    "OfflineBenchmarkAdapter",
    "OllamaBenchmarkAdapter",
    "build_benchmark_adapter",
    "default_benchmark_cases",
    "run_local_model_benchmark",
]


def __getattr__(name: str):
    # Keep python -m bootstrap free of settings/database initialization until
    # the CLI has installed its test-owned telemetry paths.
    modules = {
        "LocalModelBenchmarkReport": "models",
        "OfflineBenchmarkAdapter": "adapters",
        "OllamaBenchmarkAdapter": "adapters",
        "build_benchmark_adapter": "adapters",
        "default_benchmark_cases": "cases",
        "run_local_model_benchmark": "runner",
    }
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(f"{__name__}.{modules[name]}"), name)
