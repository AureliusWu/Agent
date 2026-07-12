# Repository Guidelines

## Project Structure & Module Organization

The application is split into `frontend/` (React, TypeScript, PWA, and `src-tauri/`), `backend/` (FastAPI, SQLite, sandbox, context manager, model provider, Skill and MCP adapters), and `scripts/` (Windows development workflows). Backend tests live in `backend/tests/`.

When implementation begins, place application code under `src/`, tests under `tests/`, and static resources under `assets/` unless the chosen framework has an established convention. Group modules by feature or responsibility rather than creating a large collection of unrelated utility files. Update this guide and `README.md` whenever the structure changes.

## Build, Test, and Development Commands

Use the repository scripts for repeatable checks:

Current repository checks:

```bash
.\scripts\dev.ps1     # Start FastAPI and the Vite PWA.
.\scripts\test.ps1    # Run backend tests, frontend lint, and production build.
cd frontend; npm run tauri dev  # Run the Windows shell after Rust is installed.
```

The Windows desktop build requires Rust/Cargo and Visual Studio C++ Build Tools. Distinguish `cargo check` from a completed installer build.

## Coding Style & Naming Conventions

Follow the formatter and linter native to the selected language. Commit their configuration with the first source files. Until then, use UTF-8, LF line endings, final newlines, and spaces instead of tabs.

Use descriptive names: `kebab-case` for documentation and configuration filenames, and the language community’s standard convention for modules, functions, classes, and tests. Keep functions focused and comments limited to non-obvious decisions.

## Testing Guidelines

Every behavioral change should include an automated test. Use `test_<feature>.py` under `backend/tests/`; frontend changes must pass TypeScript build and lint. Cover normal behavior, failures, workspace escape, and permission boundaries. Never describe unexecuted tests as passing.

## Commit & Pull Request Guidelines

The history uses Conventional Commit style, for example `chore: initialize Agent repository`. Continue with prefixes such as `feat:`, `fix:`, `test:`, `docs:`, and `refactor:`. Keep each commit focused.

Pull requests should explain the purpose, summarize changes, list verification commands, and identify risks. Link related issues and include screenshots for visible UI changes. Ensure the branch is current and the working tree contains no secrets or generated clutter.

## Security & Configuration

Never commit `.env` files, credentials, tokens, or private keys. Provide sanitized examples through `.env.example` and document every required variable.
