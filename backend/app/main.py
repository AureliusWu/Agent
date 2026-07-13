from contextlib import asynccontextmanager
import logging
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import settings
from .database import init_db
from .logging_config import configure_logging
from .routes import chat, conversations, extensions, memories, system, tools


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    init_db()
    yield


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
        try:
            response = await call_next(request)
            logging.getLogger("agent.http").info("%s %s %s %sms", request.method, request.url.path, response.status_code, round((time.perf_counter() - started) * 1000))
            return response
        except Exception:
            logging.getLogger("agent.http").exception("%s %s failed", request.method, request.url.path)
            raise
    for router in (system.router, conversations.router, chat.router, tools.router, extensions.router, memories.router):
        application.include_router(router)
    return application


app = create_app()
