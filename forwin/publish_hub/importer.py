"""The file publish-hub's `import-forwin` loads at cutover: `python -m forwin.cli publish-hub-export`.

Before a platform switches to publish-hub, the books and chapters ForWin has already uploaded must exist
there, with their platform ids. A publication that carries its remote_id is what makes publish-hub's
second dedupe layer refuse a later `upload` of the same chapter, whatever idempotency key it comes with
(D10, R35). This module reads ForWin's bindings and writes that file. It never writes to the database.

What is exported, for one platform (R13): every work binding of the platform that has a platform book id,
whatever its route (the whole platform cuts over together). A binding without a book id is a new book; the
operator creates its destination in publish-hub. Of its chapters, those whose `publish_state` is drafted,
published or submitted and that have a platform chapter id. A binding that belongs to no project (found on
the platform by an audit, not made by ForWin) is ignored with its chapters: it has no source_ref, and
ForWin has nothing it could upload again.

    {"version": 1,
     "accounts": [{"platform", "display_name", "preferred_client_id"}],       one for the platform (R14)
     "destinations": [{"platform", "remote_id", "remote_url", "settings": {"book_name"}}],
     "publications": [{"platform", "destination_remote_id", "source_ref", "remote_id", "remote_url",
                       "official_state"}]}

This is publish-hub's v1 shape (`ImportFile` in PublishHub/app/importer.py): strict, no other key anywhere,
all three lists present even when empty, no entry named twice. `source_ref` is built by `source_ref_for`
(D2), the function submit uses, so an imported chapter and a later submit name the same publication.
`publish_state` becomes `official_state` as published -> published, drafted -> drafted and
submitted -> review_pending (R12).

The same data always gives the same bytes: every list is sorted, keys are sorted, the serialisation is
fixed and the file holds nothing of the moment it was made.

Exit codes (R51):

* 0  the file is written and nothing is left out;
* 2  nothing is written: a platform book bound by two projects, a platform chapter id or a source_ref
     named twice in a book, a chapter filed under another project, a chapter whose work binding the read
     did not see, a value publish-hub would refuse, or a file that cannot be written;
* 3  the file is written, and stderr lists the chapters that were uploaded (drafted, published or
     submitted) but that it cannot carry: they have no platform chapter id, or their binding has no
     platform book id. These could be uploaded again after the switch, so the operator checks each
     against the platform by hand: fill in its `remote_chapter_id` (and the binding's `remote_book_id`
     when that is what is missing), or publish it by hand during the drain (D10); then export again.
     The audit sync does not resolve them: it refreshes only book-level state and reads back no chapter
     ids, and only an upload receipt writes `remote_chapter_id`.

Both reads run in one REPEATABLE READ, READ ONLY transaction, so the chapters are those of the work
bindings read: a book committed while the export runs is either wholly in the file or wholly out of it.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from forwin.models.publisher import PublisherChapterBinding, PublisherWorkBinding
from forwin.publishers.platforms import SUPPORTED_PLATFORMS

from .refs import source_ref_for

SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_LISTED = 3

# R12: ForWin's publish_state -> publish-hub's official_state. A chapter in any other state is not exported.
OFFICIAL_STATE = {"published": "published", "drafted": "drafted", "submitted": "review_pending"}

# The limits of publish-hub's models (`Name`, `RemoteId`, `RemoteUrl`, `SourceRef`); a longer value
# would make it refuse the whole file.
MAX_NAME = 255
MAX_REMOTE_ID = 512
MAX_REMOTE_URL = 2048
MAX_SOURCE_REF = 255


class ExportRefused(Exception):
    """The data cannot be exported as it is. Nothing is written. `problems` says why, one line each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class _Work:
    id: str
    project_id: str
    remote_id: str
    remote_url: str
    name: str


@dataclass(frozen=True)
class _Chapter:
    work_id: str
    project_id: str
    number: int
    remote_id: str
    remote_url: str
    title: str
    state: str


@dataclass(frozen=True)
class LeftOut:
    """An uploaded chapter the file cannot carry."""

    platform: str
    project_id: str
    book_name: str
    number: int
    title: str
    state: str
    reason: str

    def line(self) -> str:
        book = f" book={self.book_name!r}" if self.book_name else ""
        return (
            f"{self.platform} project={self.project_id}{book} chapter {self.number} "
            f"{self.title!r} [{self.state}]: {self.reason}"
        )


@dataclass(frozen=True)
class Export:
    document: dict[str, Any]
    left_out: list[LeftOut]
    ignored_bindings: int = 0  # work bindings of the platform that belong to no project

    @property
    def books(self) -> int:
        return len(self.document["destinations"])

    @property
    def chapters(self) -> int:
        return len(self.document["publications"])

    def render(self) -> str:
        """The file's text. Fixed: sorted keys, two-space indent, UTF-8 as it is, one final newline."""
        return json.dumps(self.document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def normalize_client_id(value: str) -> str:
    """The preferred client id as the file carries it. Raises ValueError for one publish-hub would refuse."""
    client_id = str(value).strip()
    if not client_id:
        raise ValueError("the preferred client id must not be blank")
    if len(client_id) > MAX_NAME:
        raise ValueError(f"the preferred client id is longer than {MAX_NAME} characters")
    return client_id


def _clean(value: object) -> str:
    return str(value or "").strip()


def _read_bindings(session: Session, platform: str) -> tuple[list[_Work], list[_Chapter]]:
    """The platform's work bindings, and the chapter bindings in an uploaded state with a real number."""
    works = [
        _Work(
            id=row.id, project_id=_clean(row.project_id), remote_id=_clean(row.remote_book_id),
            remote_url=_clean(row.remote_url), name=_clean(row.book_name),
        )
        for row in session.scalars(
            select(PublisherWorkBinding).where(PublisherWorkBinding.platform_id == platform)
        )
    ]
    chapters = [
        _Chapter(
            work_id=row.work_binding_id, project_id=_clean(row.project_id), number=int(row.chapter_number),
            remote_id=_clean(row.remote_chapter_id), remote_url=_clean(row.remote_url),
            title=_clean(row.chapter_title), state=row.publish_state,
        )
        for row in session.scalars(
            select(PublisherChapterBinding)
            .join(PublisherWorkBinding, PublisherChapterBinding.work_binding_id == PublisherWorkBinding.id)
            .where(
                PublisherWorkBinding.platform_id == platform,
                PublisherChapterBinding.publish_state.in_(tuple(OFFICIAL_STATE)),
                PublisherChapterBinding.chapter_number > 0,
            )
        )
    ]
    return works, chapters


def build_export(
    session: Session, *, platform: str, preferred_client_id: str | None = None
) -> Export:
    """The export of one platform, read inside a read-only transaction. Raises ExportRefused."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ExportRefused([f"unsupported platform {platform!r}"])
    try:
        client_id = None if preferred_client_id is None else normalize_client_id(preferred_client_id)
    except ValueError as exc:
        raise ExportRefused([str(exc)]) from None
    # One snapshot for both reads (the chapters belong to the work bindings read), and the database
    # itself refuses a write from here on, so nothing in the export can change a row.
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    works, chapters = _read_bindings(session, platform)
    return _plan(platform, client_id, works, chapters)


def _too_long(label: str, value: str, limit: int) -> str | None:
    return f"{label} is {len(value)} characters; publish-hub takes {limit}" if len(value) > limit else None


def _plan(
    platform: str, client_id: str | None, works: Iterable[_Work], chapters: Iterable[_Chapter]
) -> Export:
    problems: list[str] = []
    all_works = list(works)
    works_by_id = {work.id: work for work in all_works if work.project_id}  # source_ref needs a project

    exported = [work for work in works_by_id.values() if work.remote_id]
    by_book: dict[str, list[_Work]] = defaultdict(list)
    for work in exported:
        by_book[work.remote_id].append(work)
    for book_id, owners in sorted(by_book.items()):
        if len(owners) > 1:
            problems.append(
                f"{platform} book {book_id!r} is bound by {len(owners)} projects: "
                + ", ".join(f"{w.project_id} (binding {w.id})" for w in sorted(owners, key=lambda w: w.project_id))
            )
    for work in exported:
        for label, value, limit in (
            ("the book id", work.remote_id, MAX_REMOTE_ID),
            ("the book url", work.remote_url, MAX_REMOTE_URL),
        ):
            if (message := _too_long(label, value, limit)) is not None:
                problems.append(f"{platform} project {work.project_id}: {message}")

    destinations = [
        {
            "platform": platform,
            "remote_id": work.remote_id,
            "remote_url": work.remote_url or None,
            # Only the name, and only when there is one: the importer lets the file's keys win over the hub's.
            "settings": {"book_name": work.name} if work.name else {},
        }
        for work in exported
    ]

    publications: list[tuple[tuple[str, int], dict[str, Any]]] = []  # sort key: (book, chapter number)
    left_out: list[LeftOut] = []
    chapter_ids: dict[tuple[str, str], list[_Chapter]] = defaultdict(list)
    source_refs: dict[tuple[str, str], list[_Chapter]] = defaultdict(list)
    all_work_ids = {work.id for work in all_works}
    for chapter in chapters:
        work = works_by_id.get(chapter.work_id)
        if work is None:
            if chapter.work_id not in all_work_ids:
                # One snapshot cannot give this; never drop an uploaded chapter without a word.
                problems.append(
                    f"{platform} chapter {chapter.number} {chapter.title!r} (project {chapter.project_id or '-'}): "
                    f"its work binding {chapter.work_id} was not read; export again"
                )
            continue  # otherwise a binding of no project, ignored with its chapters
        if not work.remote_id:
            reason = "its binding has no platform book id"
        elif not chapter.remote_id:
            reason = "it has no platform chapter id"
        else:
            reason = ""
        if reason:
            left_out.append(
                LeftOut(
                    platform=platform, project_id=work.project_id, book_name=work.name, number=chapter.number,
                    title=chapter.title, state=chapter.state, reason=reason,
                )
            )
            continue
        where = f"{platform} project {work.project_id} chapter {chapter.number} {chapter.title!r}"
        if chapter.project_id and chapter.project_id != work.project_id:
            problems.append(
                f"{where}: filed under project {chapter.project_id}, but its book is bound to {work.project_id}"
            )
            continue
        source_ref = source_ref_for(work.project_id, chapter.number)  # D2: never built any other way
        for label, value, limit in (
            ("the source_ref", source_ref, MAX_SOURCE_REF),
            ("the chapter id", chapter.remote_id, MAX_REMOTE_ID),
            ("the chapter url", chapter.remote_url, MAX_REMOTE_URL),
        ):
            if (message := _too_long(label, value, limit)) is not None:
                problems.append(f"{where}: {message}")
        chapter_ids[(work.remote_id, chapter.remote_id)].append(chapter)
        source_refs[(work.remote_id, source_ref)].append(chapter)
        publications.append(
            (
                (work.remote_id, chapter.number),
                {
                    "platform": platform,
                    "destination_remote_id": work.remote_id,
                    "source_ref": source_ref,
                    "remote_id": chapter.remote_id,
                    "remote_url": chapter.remote_url or None,
                    "official_state": OFFICIAL_STATE[chapter.state],
                },
            )
        )

    for (book_id, chapter_id), held in sorted(chapter_ids.items()):
        if len(held) > 1:
            problems.append(
                f"{platform} book {book_id!r}: platform chapter id {chapter_id!r} is held by "
                + ", ".join(f"chapter {c.number} {c.title!r}" for c in sorted(held, key=lambda c: c.number))
            )
    for (book_id, source_ref), held in sorted(source_refs.items()):
        if len(held) > 1:
            problems.append(
                f"{platform} book {book_id!r}: {source_ref} is held by {len(held)} chapter bindings "
                f"({', '.join(repr(c.remote_id) for c in sorted(held, key=lambda c: c.remote_id))})"
            )
    if problems:
        raise ExportRefused(problems)

    # Chapter 2 before chapter 10, which the text of the source_ref would not give.
    publications.sort(key=lambda item: item[0])
    destinations.sort(key=lambda d: d["remote_id"])
    left_out.sort(key=lambda c: (c.project_id, c.number, c.title, c.state))
    account = {
        "platform": platform,
        "display_name": SUPPORTED_PLATFORMS[platform].display_name,
        "preferred_client_id": client_id,
    }
    return Export(
        document={
            "version": SCHEMA_VERSION,
            "accounts": [account],
            "destinations": destinations,
            "publications": [publication for _, publication in publications],
        },
        left_out=left_out,
        ignored_bindings=len(all_works) - len(works_by_id),
    )


def export_to_file(
    session_factory: sessionmaker[Session],
    *,
    platform: str,
    out: str | Path,
    preferred_client_id: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Write the platform's export to `out` and return the exit code (see the module docstring)."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        with session_factory() as session:
            export = build_export(session, platform=platform, preferred_client_id=preferred_client_id)
    except ExportRefused as exc:
        print(f"publish-hub-export: refused, nothing written ({len(exc.problems)}):", file=stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=stderr)
        return EXIT_REFUSED

    try:
        Path(out).write_bytes(export.render().encode("utf-8"))
    except OSError as exc:
        print(f"publish-hub-export: cannot write {str(out)!r}: {exc.strerror or exc}", file=stderr)
        return EXIT_REFUSED
    print(
        f"publish-hub-export: wrote {_count(export.books, 'book')} and {_count(export.chapters, 'chapter')} "
        f"of {platform} to {out}",
        file=stdout,
    )
    if export.ignored_bindings:
        print(
            f"publish-hub-export: ignored {_count(export.ignored_bindings, 'binding')} of no project "
            "(not made by ForWin, nothing for it to upload again)",
            file=stdout,
        )
    if not export.left_out:
        return EXIT_OK
    print(
        f"publish-hub-export: {_count(len(export.left_out), 'uploaded chapter')} cannot be exported and could "
        "be uploaded again after the switch:",
        file=stderr,
    )
    for left in export.left_out:
        print(f"  {left.line()}", file=stderr)
    print(
        "publish-hub-export: check each on the platform by hand: fill in its remote_chapter_id (and the "
        "binding's remote_book_id when that is missing), or publish it by hand during the drain (D10), then "
        "export again. The audit sync refreshes only book-level state and reads back no chapter ids, so it "
        "does not resolve these.",
        file=stderr,
    )
    return EXIT_LISTED


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


__all__ = [
    "EXIT_LISTED",
    "EXIT_OK",
    "EXIT_REFUSED",
    "OFFICIAL_STATE",
    "SCHEMA_VERSION",
    "Export",
    "ExportRefused",
    "LeftOut",
    "build_export",
    "export_to_file",
    "normalize_client_id",
]
