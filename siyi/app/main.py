import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import settings
from .database import init_db
from app.personality.identity_service import ensure_identity_kernel
from app.personality.affect import ensure_affect_state
from .deployment import validate_deployment_security
from .logging_config import configure_logging
from app.security.request_security import valid_api_token
from .api.routes import agents, backups, chat, commands, conversations, extensions, identity, local_models, long_term_memories, memories, search, state, stt, system, tools, tts, vision, voice
from app.runtime.task_runtime import start_task_runtime, stop_task_runtime
from app.artifacts.title_jobs import start_title_runtime, stop_title_runtime


async def _run_tts_temporary_audio_reaper() -> None:
    """Bound non-cache WAV retention even when a renderer vanishes silently."""
    from app.tts.manager import tts_manager

    while True:
        await asyncio.sleep(tts_manager.temporary_audio_reaper_interval_seconds)
        try:
            await tts_manager.reap_expired_temporary_audio()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("agent.tts").exception("temporary TTS audio reaper failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_deployment_security()
    configure_logging()
    init_db()
    from app.voice.session_manager import voice_session_manager
    from app.tts.manager import tts_manager

    voice_session_manager.recover_interrupted_sessions()
    tts_manager.cleanup_orphaned_temporary_audio()
    ensure_identity_kernel()
    ensure_affect_state()
    await start_task_runtime()
    await start_title_runtime()
    tts_reaper = asyncio.create_task(_run_tts_temporary_audio_reaper())
    try:
        yield
    finally:
        tts_reaper.cancel()
        with suppress(asyncio.CancelledError):
            await tts_reaper
        await stop_task_runtime()
        await stop_title_runtime()
        from app.voice.session_manager import voice_session_manager
        await voice_session_manager.shutdown()
        from app.stt.manager import stt_manager
        await stt_manager.shutdown()
        from app.tts.manager import tts_manager
        await tts_manager.shutdown()
        from app.local_runtime.ollama_service_manager import ollama_service_manager
        await ollama_service_manager().shutdown()


def create_app() -> FastAPI:
    application = FastAPI(title="Agent API", version=__version__, lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    @application.middleware("http")
    async def request_log(request, call_next):
        started = time.perf_counter()
        if (
            request.method != "OPTIONS"
            and request.url.path != "/api/health"
            and not valid_api_token(request.headers.get("x-agent-api-token"))
        ):
            logging.getLogger("agent.security").warning("rejected unauthenticated local API request: %s %s", request.method, request.url.path)
            return JSONResponse({"detail": "本地 API 令牌无效"}, status_code=401)
        try:
            response = await call_next(request)
            logging.getLogger("agent.http").info("%s %s %s %sms", request.method, request.url.path, response.status_code, round((time.perf_counter() - started) * 1000))
            return response
        except Exception:
            logging.getLogger("agent.http").exception("%s %s failed", request.method, request.url.path)
            raise
    for router in (system.router, agents.router, identity.router, state.router, backups.router, conversations.router, chat.router, commands.router, tools.router, vision.router, extensions.router, local_models.router, tts.router, stt.router, voice.router, search.router, memories.router, long_term_memories.router):
        application.include_router(router)
    return application


app = create_app()
