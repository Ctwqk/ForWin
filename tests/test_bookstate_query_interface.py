from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.models.base import Base
from forwin.models.canon_quality import CountdownLedgerRow


def test_bookstate_query_interface_returns_latest_countdown_as_of_chapter() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        session.add_all(
            [
                CountdownLedgerRow(project_id="p1", countdown_key="main", chapter_number=2, normalized_remaining_minutes=79),
                CountdownLedgerRow(project_id="p1", countdown_key="main", chapter_number=17, normalized_remaining_minutes=57),
                CountdownLedgerRow(project_id="p1", countdown_key="hidden", chapter_number=17, normalized_remaining_minutes=16),
            ]
        )

    with Session() as session:
        values = SqlBookStateQueryInterface(session).get_current_countdown_values(
            project_id="p1",
            as_of_chapter=17,
        )

    assert values["main"].remaining_minutes == 57
    assert values["hidden"].remaining_minutes == 16
