# Repository Guidelines

## Agent Working Rules

Operate only inside the user-selected workspace. Preserve the three permission modes: `ask` requests approval for writes, `agent` may perform ordinary workspace edits, and `full` permits workspace file changes while still confirming critical commands and external MCP calls. Never weaken path normalization, symlink checks, audit logging, or secret handling.

## Project Structure

Backend code lives in `backend/app/`. Keep `main.py` limited to application assembly, HTTP endpoints in `routes/`, orchestration in `task_runner.py`, and domain logic in focused modules such as `sandbox.py`, `provider.py`, `mcp.py`, and `skills.py`. Backend tests use `backend/tests/test_<feature>.py`.

Frontend code lives in `frontend/src/`. Put reusable UI in `components/`, stateful behavior in `hooks/`, and component styles in `styles/`. The Windows shell is under `frontend/src-tauri/`. Do not rebuild a large all-purpose `App.tsx` or global stylesheet.

## Verification Commands

Run the repository workflows from PowerShell:

```powershell
.\scripts\dev.ps1       # Start FastAPI and Vite; creates backend/.env when absent
.\scripts\test.ps1      # Backend tests, frontend lint, and production build
.\scripts\clean.ps1     # Remove reproducible build artifacts
```

For desktop changes, also run `cd frontend; cargo check --manifest-path src-tauri/Cargo.toml`. A release is not complete until the relevant API, PWA, and cancellation path have been exercised.

## Coding And Tests

Use Python 3.12, type hints, four-space indentation, and UTF-8. Use TypeScript strict mode, functional React components, and descriptive `PascalCase` component names. Keep public API behavior backward compatible unless the change is documented.

Every behavior change needs a focused test. Mock network providers and MCP processes; never use a real credential in tests. Cancellation tests must prove that an in-flight model wait is interrupted, not merely marked cancelled afterward.

## Commits And Security

Use Conventional Commits such as `fix: interrupt running agent tasks`. Do not commit `.env`, databases, credentials, generated installers, `build/`, `dist/`, or Rust `target/` output. Update `README.md` for user-facing behavior and this file only when contributor rules change.
