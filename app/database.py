from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine


DATABASE_PATH = Path(__file__).resolve().parent / "storage" / "app.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(_engine)


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(_engine) as session:
        yield session
