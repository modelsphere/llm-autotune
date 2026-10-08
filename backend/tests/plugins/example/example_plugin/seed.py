"""What the example plugin needs on a fresh install, re-checked on every deploy."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from example_plugin.models import Setting


def seed(engine: Engine) -> None:
    # Only ever add what is missing: someone may have changed the value since.
    with Session(engine) as session:
        if session.get(Setting, "greeting") is None:
            session.add(Setting(key="greeting", value="hello"))
            session.commit()
