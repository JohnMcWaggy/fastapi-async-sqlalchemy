from contextvars import ContextVar
from typing import Callable, Dict, Optional, Union

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.types import ASGIApp

from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError

SessionEventBinder = Callable[[sessionmaker], None]

_Session: Optional[sessionmaker] = None
_session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)


class SQLAlchemyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        db_url: Optional[Union[str, URL]] = None,
        custom_engine: Optional[Engine] = None,
        engine_args: Optional[Dict] = None,
        session_args: Optional[Dict] = None,
        commit_on_exit: bool = False,
        bind_events: Optional[SessionEventBinder] = None,
    ):
        super().__init__(app)
        self.commit_on_exit = commit_on_exit
        engine_args = engine_args or {}
        session_args = session_args or {}

        if not custom_engine and not db_url:
            raise ValueError("You need to pass a db_url or a custom_engine parameter.")
        if not custom_engine:
            engine = create_async_engine(db_url, **engine_args)
        else:
            engine = custom_engine

        global _Session
        _Session = sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            sync_session_class=Session,
            **session_args,
        )

        if bind_events:
            bind_events(_Session)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        async with db(commit_on_exit=self.commit_on_exit):
            return await call_next(request)


class DBSessionMeta(type):
    # using this metaclass means that we can access db.session as a property at a class level,
    # rather than db().session
    @property
    def session(self) -> AsyncSession:
        """Return an instance of Session local to the current async context."""
        if _Session is None:
            raise SessionNotInitialisedError

        session = _session.get()
        if session is None:
            raise MissingSessionError

        return session


class DBSession(metaclass=DBSessionMeta):
    def __init__(self, session_args: Dict = None, commit_on_exit: bool = False):
        self.token = None
        self.session_args = session_args or {}
        self.commit_on_exit = commit_on_exit

    async def _init_session(self):
        self.token = _session.set(_Session(**self.session_args))  # type: ignore

    async def __aenter__(self):
        if not isinstance(_Session, sessionmaker):
            raise SessionNotInitialisedError

        await self._init_session()
        return type(self)

    async def __aexit__(self, exc_type, exc_value, traceback):
        session = _session.get()
        if exc_type is not None:
            await session.rollback()

        if self.commit_on_exit:
            await session.commit()

        await session.close()
        _session.reset(self.token)


db: DBSessionMeta = DBSession
