from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.book_frame import BookLexiconRow, StoryCalendarRow

from .lexicon import EMPTY_LEXICON, BookLexicon, lexicon_from_payload, lexicon_to_payload
from .story_clock import StoryCalendar, calendar_from_payload, calendar_to_payload


class StoryCalendarRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, project_id: str) -> StoryCalendar | None:
        row = self.session.scalar(select(StoryCalendarRow).where(StoryCalendarRow.project_id == project_id))
        if row is None:
            return None
        return calendar_from_payload({
            "epoch_label": row.epoch_label,
            "opening_minute": row.opening_minute,
            "units": json.loads(row.units_json),
        })

    def save(self, project_id: str, calendar: StoryCalendar) -> None:
        payload = calendar_to_payload(calendar)
        row = self.session.scalar(select(StoryCalendarRow).where(StoryCalendarRow.project_id == project_id))
        if row is None:
            row = StoryCalendarRow(project_id=project_id)
            self.session.add(row)
        row.epoch_label = payload["epoch_label"]
        row.opening_minute = payload["opening_minute"]
        row.units_json = json.dumps(payload["units"], ensure_ascii=False)
        self.session.flush()


class BookLexiconRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, project_id: str) -> BookLexicon:
        row = self.session.scalar(select(BookLexiconRow).where(BookLexiconRow.project_id == project_id))
        if row is None:
            return EMPTY_LEXICON
        return lexicon_from_payload(json.loads(row.lexicon_json))

    def save(self, project_id: str, lexicon: BookLexicon) -> None:
        row = self.session.scalar(select(BookLexiconRow).where(BookLexiconRow.project_id == project_id))
        if row is None:
            row = BookLexiconRow(project_id=project_id)
            self.session.add(row)
        row.lexicon_json = json.dumps(lexicon_to_payload(lexicon), ensure_ascii=False)
        self.session.flush()
