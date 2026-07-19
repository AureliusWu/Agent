# Siyi execution core

`siyi/` contains the provider-neutral Agent API and execution runtime. It owns planning, tools, workspace access, queues, permissions, recovery, verification, and observability. Personality-specific behavior belongs in `kokoro/` and shared boundaries belong in `contracts/`.
