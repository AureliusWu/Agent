# Archived voice-provider prototype (not a production module)

Preserved from `feature/voice-provider-prototype`, commit
`e27ea0d1fd2ed03b3c1538af7613d690c04f349e`, during the 2026-10-03
main-branch consolidation. The original added source files are kept byte-for-byte
under their original relative paths inside this directory.

This example is **not installed, imported, registered, or run by the application**.
Its archived tests are excluded from automatic pytest collection by the local
`conftest.py`; they describe the historical mock prototype, not today's app.
Its mock STT/TTS contracts are historical reference only. Do not copy its
`main.py`/routing design over the current implementation: production voice lives
in `siyi/app/voice`, `siyi/app/stt`, `siyi/app/tts`, and their current API routes.

The original branch's README and modified application assembly, full Git
history, and the separate consolidation-backup branch are additionally retained
in the administrator's local private synchronization backup, not in this public
source tree. No runtime data or credentials are included here.
