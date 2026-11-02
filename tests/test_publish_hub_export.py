"""Task 10: `publish-hub-export` writes the file publish-hub's `import-forwin` loads at cutover.

The books and chapters ForWin has already uploaded must exist in publish-hub before the switch, so
that its second dedupe layer refuses a re-upload. The file is the v1 shape of publish-hub's
`app/importer.py`; `_assert_hub_v1` below is a local copy of that parser's rules, since ForWin
cannot import it.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text

from forwin.cli import build_parser, main
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import PublisherChapterBinding, PublisherWorkBinding
from forwin.publish_hub import importer as export_module
from forwin.publish_hub.importer import export_to_file
from forwin.publish_hub.refs import source_ref_for
from tests.postgres import postgres_test_url

# --- a local copy of publish-hub's v1 rules --------------------------------------------------------
#
# Keep in sync with PublishHub/app/importer.py: the models `ImportFile`, `AccountEntry`,
# `DestinationEntry` and `PublicationEntry` (strict, extra="forbid", no blank text, the length limits
# of `Name`, `RemoteId`, `RemoteUrl` and `SourceRef`), `parse_import_file` (version 1, all three
# lists required, text that Postgres can store) and `_refuse_repeats` (no entry named twice).

HUB_PLATFORMS = ("fanqie", "qidian")
HUB_STATES = ("published", "drafted", "review_pending")
HUB_NAME, HUB_REMOTE_ID, HUB_REMOTE_URL, HUB_SOURCE_REF = 255, 512, 2048, 255
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def _hub_text(value: object, limit: int, where: str) -> None:
    assert type(value) is str, f"{where}: must be a string"
    assert value.strip() != "", f"{where}: must not be blank"
    assert 1 <= len(value) <= limit, f"{where}: longer than {limit}"


def _hub_entry(entry: object, where: str, required: set[str], optional: set[str]) -> dict:
    assert isinstance(entry, dict), f"{where}: must be an object"
    assert required <= set(entry), f"{where}: missing {sorted(required - set(entry))}"
    assert set(entry) <= required | optional, f"{where}: extra {sorted(set(entry) - required - optional)}"
    assert entry["platform"] in HUB_PLATFORMS, f"{where}.platform: unknown"
    return entry


def _assert_hub_v1(text_: str) -> dict:
    assert not _UNSTORABLE.search(text_)
    raw = json.loads(text_)
    assert isinstance(raw, dict)
    assert set(raw) == {"version", "accounts", "destinations", "publications"}
    assert type(raw["version"]) is int and raw["version"] == 1
    for name in ("accounts", "destinations", "publications"):
        assert isinstance(raw[name], list), f"{name} must be a list"

    for i, item in enumerate(raw["accounts"]):
        entry = _hub_entry(item, f"accounts[{i}]", {"platform", "display_name"}, {"preferred_client_id"})
        _hub_text(entry["display_name"], HUB_NAME, f"accounts[{i}].display_name")
        if entry.get("preferred_client_id") is not None:
            _hub_text(entry["preferred_client_id"], HUB_NAME, f"accounts[{i}].preferred_client_id")
    for i, item in enumerate(raw["destinations"]):
        entry = _hub_entry(item, f"destinations[{i}]", {"platform", "remote_id"}, {"remote_url", "settings"})
        _hub_text(entry["remote_id"], HUB_REMOTE_ID, f"destinations[{i}].remote_id")
        if entry.get("remote_url") is not None:
            _hub_text(entry["remote_url"], HUB_REMOTE_URL, f"destinations[{i}].remote_url")
        assert isinstance(entry.get("settings", {}), dict), f"destinations[{i}].settings: must be an object"
    for i, item in enumerate(raw["publications"]):
        entry = _hub_entry(
            item,
            f"publications[{i}]",
            {"platform", "destination_remote_id", "source_ref", "remote_id", "official_state"},
            {"remote_url"},
        )
        _hub_text(entry["destination_remote_id"], HUB_REMOTE_ID, f"publications[{i}].destination_remote_id")
        _hub_text(entry["source_ref"], HUB_SOURCE_REF, f"publications[{i}].source_ref")
        _hub_text(entry["remote_id"], HUB_REMOTE_ID, f"publications[{i}].remote_id")
        if entry.get("remote_url") is not None:
            _hub_text(entry["remote_url"], HUB_REMOTE_URL, f"publications[{i}].remote_url")
        assert entry["official_state"] in HUB_STATES, f"publications[{i}].official_state: unknown"

    def no_repeats(label: str, keys: list[tuple]) -> None:
        assert len(set(keys)) == len(keys), f"{label} names a row twice"

    no_repeats("accounts", [(e["platform"],) for e in raw["accounts"]])
    no_repeats("destinations", [(e["platform"], e["remote_id"]) for e in raw["destinations"]])
    no_repeats(
        "publications",
        [(e["platform"], e["destination_remote_id"], e["source_ref"]) for e in raw["publications"]],
    )
    # The importer resolves these against the hub too; an export must be complete without the hub.
    accounts = {e["platform"] for e in raw["accounts"]}
    books = {(e["platform"], e["remote_id"]) for e in raw["destinations"]}
    assert {e["platform"] for e in raw["destinations"]} <= accounts
    assert {(e["platform"], e["destination_remote_id"]) for e in raw["publications"]} <= books
    return raw


def test_the_local_v1_check_has_teeth() -> None:
    good = {"version": 1, "accounts": [], "destinations": [], "publications": []}
    _assert_hub_v1(json.dumps(good))
    bad = [
        {**good, "extra": 1},
        {**good, "version": 2},
        {**good, "version": True},
        {k: v for k, v in good.items() if k != "publications"},
        {**good, "accounts": [{"platform": "fanqie", "display_name": "x", "colour": "red"}]},
        {**good, "accounts": [{"platform": "weibo", "display_name": "x"}]},
        {**good, "destinations": [{"platform": "fanqie", "remote_id": " "}]},
        {**good, "destinations": [{"platform": "fanqie", "remote_id": "1", "settings": []}]},
        {**good, "accounts": [{"platform": "fanqie", "display_name": "a"}] * 2},
        {
            **good,
            "accounts": [{"platform": "fanqie", "display_name": "a"}],
            "publications": [
                {"platform": "fanqie", "destination_remote_id": "1", "source_ref": "p", "remote_id": "2",
                 "official_state": "published"}
            ],
        },
        {
            **good,
            "accounts": [{"platform": "fanqie", "display_name": "a"}],
            "destinations": [{"platform": "fanqie", "remote_id": "1"}],
            "publications": [
                {"platform": "fanqie", "destination_remote_id": "1", "source_ref": "p", "remote_id": "2",
                 "official_state": "submitted"}
            ],
        },
    ]
    for document in bad:
        with pytest.raises(AssertionError):
            _assert_hub_v1(json.dumps(document))


# --- the database ----------------------------------------------------------------------------------


@dataclass
class Db:
    url: str
    engine: object
    session_factory: object


@pytest.fixture(scope="module")
def _database():
    """One database for the module: creating one per test costs more than the tests do."""
    url = postgres_test_url("publish-hub-export", keep_until_exit=True)
    engine = get_engine(url)
    init_db(engine)
    try:
        yield Db(url=url, engine=engine, session_factory=get_session_factory(engine))
    finally:
        engine.dispose()


@pytest.fixture()
def db(_database):
    with _database.engine.begin() as conn:
        conn.execute(text("TRUNCATE publisher_chapter_bindings, publisher_work_bindings, projects CASCADE"))
    return _database


def add_book(
    db: Db,
    *,
    project: str,
    book_id: str | None,
    platform: str = "fanqie",
    name: str = "",
    url: str = "",
    chapters: tuple[tuple, ...] = (),
    raw: dict | None = None,
) -> str:
    """One work binding with its chapter bindings; chapters are (number, remote id, state[, title[, url]])."""
    work_id = new_id()
    with db.session_factory.begin() as session:
        session.add(
            PublisherWorkBinding(
                id=work_id, project_id=project, platform_id=platform, book_name=name,
                remote_book_id=book_id or "", remote_url=url,
                raw_payload_json=json.dumps(raw or {}, ensure_ascii=False),
            )
        )
        session.flush()
        for number, remote_id, state, *more in chapters:
            session.add(
                PublisherChapterBinding(
                    work_binding_id=work_id, project_id=project, platform_id=platform,
                    chapter_number=number, remote_chapter_id=remote_id or "", publish_state=state,
                    chapter_title=more[0] if more else "", remote_url=more[1] if len(more) > 1 else "",
                )
            )
    return work_id


def run(db: Db, out: Path, platform: str = "fanqie", **kwargs) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = export_to_file(
        db.session_factory, platform=platform, out=out, stdout=stdout, stderr=stderr, **kwargs
    )
    return code, stdout.getvalue(), stderr.getvalue()


def exported(out: Path) -> dict:
    return _assert_hub_v1(out.read_text(encoding="utf-8"))


# --- what is exported ------------------------------------------------------------------------------


def test_exports_books_and_chapters_in_the_v1_shape(db, tmp_path) -> None:
    add_book(
        db, project="p1", book_id="B1", name="第一本书", url="https://fanqie.example/book/B1",
        chapters=((1, "C1", "published", "起", "https://fanqie.example/c/C1"), (2, "C2", "drafted", "承")),
    )
    out = tmp_path / "export.json"

    code, stdout, stderr = run(db, out)

    assert (code, stderr) == (0, "")
    assert "1 book" in stdout and "2 chapter" in stdout
    assert exported(out) == {
        "version": 1,
        "accounts": [{"platform": "fanqie", "display_name": "番茄小说", "preferred_client_id": None}],
        "destinations": [
            {"platform": "fanqie", "remote_id": "B1", "remote_url": "https://fanqie.example/book/B1",
             "settings": {"book_name": "第一本书"}}
        ],
        "publications": [
            {"platform": "fanqie", "destination_remote_id": "B1", "source_ref": "project/p1/chapter/1",
             "remote_id": "C1", "remote_url": "https://fanqie.example/c/C1", "official_state": "published"},
            {"platform": "fanqie", "destination_remote_id": "B1", "source_ref": "project/p1/chapter/2",
             "remote_id": "C2", "remote_url": None, "official_state": "drafted"},
        ],
    }


def test_only_the_requested_platform_is_exported(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    add_book(db, project="p2", book_id="Q1", platform="qidian", chapters=((1, "QC1", "published"),))

    assert run(db, tmp_path / "f.json", "fanqie")[0] == 0
    assert run(db, tmp_path / "q.json", "qidian")[0] == 0

    fanqie, qidian = exported(tmp_path / "f.json"), exported(tmp_path / "q.json")
    assert [a["platform"] for a in fanqie["accounts"]] == ["fanqie"]
    assert [d["remote_id"] for d in fanqie["destinations"]] == ["B1"]
    assert [p["remote_id"] for p in fanqie["publications"]] == ["C1"]
    assert [a["platform"] for a in qidian["accounts"]] == ["qidian"]
    assert [d["remote_id"] for d in qidian["destinations"]] == ["Q1"]
    assert [p["remote_id"] for p in qidian["publications"]] == ["QC1"]
    assert qidian["accounts"][0]["display_name"] == "起点小说"


def test_the_route_does_not_decide_what_is_exported(db, tmp_path) -> None:
    routes = {"hub-project": "hub", "legacy-project": "legacy", "bare-project": None}
    with db.session_factory.begin() as session:
        for project_id, route in routes.items():
            automation = {} if route is None else {"publish_bindings": [{"platform": "fanqie", "route": route}]}
            session.add(
                Project(
                    id=project_id, title=project_id, premise="p", genre="g",
                    automation_json=json.dumps(automation),
                )
            )
    for index, project_id in enumerate(routes):
        add_book(db, project=project_id, book_id=f"B{index}", chapters=((1, f"C{index}", "published"),))

    code, _, stderr = run(db, tmp_path / "out.json")

    assert (code, stderr) == (0, "")
    document = exported(tmp_path / "out.json")
    assert [d["remote_id"] for d in document["destinations"]] == ["B0", "B1", "B2"]
    assert len(document["publications"]) == 3


@pytest.mark.parametrize("book_id", [None, "", "   "])
def test_a_binding_without_a_book_id_is_not_exported(db, tmp_path, book_id) -> None:
    add_book(db, project="new-book", book_id=book_id)
    add_book(db, project="old-book", book_id="B1", chapters=((1, "C1", "published"),))

    code, _, stderr = run(db, tmp_path / "out.json")

    assert (code, stderr) == (0, "")
    assert [d["remote_id"] for d in exported(tmp_path / "out.json")["destinations"]] == ["B1"]


@pytest.mark.parametrize(
    ("state", "official"),
    [("published", "published"), ("drafted", "drafted"), ("submitted", "review_pending")],
)
def test_publish_state_maps_to_the_hub_official_state(db, tmp_path, state, official) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", state),))

    assert run(db, tmp_path / "out.json")[0] == 0

    assert [p["official_state"] for p in exported(tmp_path / "out.json")["publications"]] == [official]


@pytest.mark.parametrize("state", ["unknown", "", "failed", "unpublished", "scheduled", "Published"])
def test_chapters_in_other_states_are_ignored(db, tmp_path, state) -> None:
    add_book(
        db, project="p1", book_id="B1",
        chapters=((1, "C1", "published"), (2, "C2", state), (3, "", state)),
    )

    code, _, stderr = run(db, tmp_path / "out.json")

    assert (code, stderr) == (0, "")
    assert [p["remote_id"] for p in exported(tmp_path / "out.json")["publications"]] == ["C1"]


def test_chapter_number_zero_is_not_a_chapter(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((0, "C0", "published"), (0, "", "drafted"), (1, "C1", "published")))

    code, _, stderr = run(db, tmp_path / "out.json")

    assert (code, stderr) == (0, "")
    assert [p["source_ref"] for p in exported(tmp_path / "out.json")["publications"]] == ["project/p1/chapter/1"]


def test_a_platform_with_nothing_to_export_still_writes_all_three_lists(db, tmp_path) -> None:
    code, stdout, stderr = run(db, tmp_path / "out.json")

    assert (code, stderr) == (0, "")
    assert "0 book" in stdout
    document = exported(tmp_path / "out.json")
    assert document["destinations"] == [] and document["publications"] == []
    assert [a["platform"] for a in document["accounts"]] == ["fanqie"]


# --- the account, the destination, the publication -------------------------------------------------


def test_the_preferred_client_id_goes_on_the_one_account(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1")
    add_book(db, project="p2", book_id="B2")

    assert run(db, tmp_path / "a.json", preferred_client_id="magi3-ext-1")[0] == 0
    assert run(db, tmp_path / "b.json")[0] == 0

    accounts = exported(tmp_path / "a.json")["accounts"]
    assert accounts == [{"platform": "fanqie", "display_name": "番茄小说", "preferred_client_id": "magi3-ext-1"}]
    assert exported(tmp_path / "b.json")["accounts"][0]["preferred_client_id"] is None


def test_a_destination_carries_nothing_but_the_book_name(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", name=" 书名 ", url=" https://x.example/B1 ", raw={"secret": "x"})
    add_book(db, project="p2", book_id="B2", name="  ", url="")

    assert run(db, tmp_path / "out.json")[0] == 0

    assert "secret" not in (tmp_path / "out.json").read_text(encoding="utf-8")
    first, second = exported(tmp_path / "out.json")["destinations"]
    assert first == {"platform": "fanqie", "remote_id": "B1", "remote_url": "https://x.example/B1",
                     "settings": {"book_name": "书名"}}
    # A blank name is left out, not sent: the importer lets the file's keys win over the hub's.
    assert second == {"platform": "fanqie", "remote_id": "B2", "remote_url": None, "settings": {}}


def test_source_ref_comes_from_source_ref_for_and_never_holds_a_candidate_id(db, tmp_path, monkeypatch) -> None:
    candidate = "cand-0f9e8d7c"
    add_book(
        db, project="proj-1", book_id="B1", chapters=((7, "C7", "published"),),
        raw={"candidate_id": candidate},
    )
    with db.session_factory.begin() as session:
        session.execute(
            text("UPDATE publisher_chapter_bindings SET raw_payload_json = :raw"),
            {"raw": json.dumps({"candidate_id": candidate, "job": {"candidate_id": candidate}})},
        )
    out = tmp_path / "out.json"
    assert run(db, out)[0] == 0
    document = exported(out)
    assert document["publications"][0]["source_ref"] == source_ref_for("proj-1", 7) == "project/proj-1/chapter/7"
    assert candidate not in out.read_text(encoding="utf-8")

    calls: list[tuple] = []

    def recording(project_id, chapter_number):
        calls.append((project_id, chapter_number))
        return f"via-source-ref-for/{project_id}/{chapter_number}"

    monkeypatch.setattr(export_module, "source_ref_for", recording)
    assert run(db, out)[0] == 0
    assert set(calls) == {("proj-1", 7)}
    assert json.loads(out.read_text(encoding="utf-8"))["publications"][0]["source_ref"] == "via-source-ref-for/proj-1/7"


# --- chapters that could be uploaded again ---------------------------------------------------------


def test_uploaded_chapters_without_a_platform_id_are_listed_and_exit_3_after_writing(db, tmp_path) -> None:
    add_book(
        db, project="p1", book_id="B1", name="书",
        chapters=((1, "C1", "published", "一"), (2, "", "drafted", "二"), (3, "  ", "submitted", "三"),
                  (4, "", "published", "四"), (5, "", "unknown", "五")),
    )
    out = tmp_path / "out.json"

    code, stdout, stderr = run(db, out)

    assert code == 3
    assert out.exists(), "the file is written even when chapters are listed"
    assert [p["remote_id"] for p in exported(out)["publications"]] == ["C1"]
    lines = [line for line in stderr.splitlines() if "chapter" in line and "p1" in line]
    assert len(lines) == 3
    for line, (number, title, state) in zip(lines, [(2, "二", "drafted"), (3, "三", "submitted"), (4, "四", "published")]):
        assert "fanqie" in line and "p1" in line
        assert f"chapter {number} " in line and title in line and state in line
    assert "五" not in stderr  # an 'unknown' chapter is not one that was uploaded
    assert "1 chapter" in stdout


def test_uploaded_chapters_of_a_binding_without_a_book_id_are_listed(db, tmp_path) -> None:
    add_book(db, project="p1", book_id=None, chapters=((1, "C1", "published", "有章"), (2, "", "drafted", "无号")))
    add_book(db, project="p2", book_id="B2", chapters=((1, "C9", "published"),))
    out = tmp_path / "out.json"

    code, _, stderr = run(db, out)

    assert code == 3
    assert [d["remote_id"] for d in exported(out)["destinations"]] == ["B2"]
    assert "有章" in stderr and "无号" in stderr
    assert stderr.count("p1") >= 2 and "p2" not in stderr


def test_a_binding_of_no_project_is_ignored_with_its_chapters_and_counted(db, tmp_path) -> None:
    # Found on the platform by an audit, not made by ForWin: no source_ref, and nothing ForWin could upload.
    add_book(db, project="", book_id="B0", chapters=((1, "C1", "published", "孤章"), (2, "", "drafted", "无号")))
    add_book(db, project="p1", book_id="B1", chapters=((1, "C9", "published"),))
    out = tmp_path / "out.json"

    code, stdout, stderr = run(db, out)

    assert (code, stderr) == (0, "")
    document = exported(out)
    assert [d["remote_id"] for d in document["destinations"]] == ["B1"]
    assert [p["remote_id"] for p in document["publications"]] == ["C9"]
    assert "ignored 1 binding of no project" in stdout


def test_the_listing_says_a_left_out_chapter_is_checked_on_the_platform_by_hand(db, tmp_path) -> None:
    """Only an upload receipt writes a chapter's platform id: the audit sync refreshes the book and
    reads back no chapter ids, so running it cannot resolve a left-out chapter (final review, item 3)."""
    add_book(db, project="p1", book_id="B1", chapters=((1, "", "drafted", "一"),))

    code, _, stderr = run(db, tmp_path / "out.json")

    assert code == 3
    guidance = stderr.split("it has no platform chapter id", 1)[1]
    assert "by hand" in guidance and "remote_chapter_id" in guidance and "D10" in guidance
    assert "The audit sync refreshes only book-level state" in guidance
    doc = " ".join((export_module.__doc__ or "").split())
    assert "run the audit sync for the binding" not in doc
    assert "checks each against the platform by hand" in doc


def test_the_listing_is_in_a_fixed_order(db, tmp_path) -> None:
    add_book(db, project="pb", book_id="B2", chapters=((2, "", "drafted", "b2"), (1, "", "drafted", "b1")))
    add_book(db, project="pa", book_id="B1", chapters=((10, "", "drafted", "a10"), (2, "", "drafted", "a2")))

    _, _, stderr = run(db, tmp_path / "out.json")

    titles = [t for t in re.findall(r"\b([ab]\d+)\b", stderr)]
    assert titles == ["a2", "a10", "b1", "b2"]


# --- refusals --------------------------------------------------------------------------------------


def test_two_projects_bound_to_one_book_are_refused_and_nothing_is_written(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="SAME", chapters=((1, "C1", "published"),))
    add_book(db, project="p2", book_id="SAME", chapters=((1, "C2", "published"),))
    add_book(db, project="p3", book_id="OTHER")
    out = tmp_path / "out.json"

    code, stdout, stderr = run(db, out)

    assert code == 2
    assert not out.exists()
    assert "SAME" in stderr and "p1" in stderr and "p2" in stderr and "p3" not in stderr
    assert stdout == ""


def test_a_refusal_leaves_an_existing_file_as_it_was(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="SAME")
    add_book(db, project="p2", book_id="SAME")
    out = tmp_path / "out.json"
    out.write_text("previous", encoding="utf-8")

    assert run(db, out)[0] == 2

    assert out.read_text(encoding="utf-8") == "previous"


def test_the_same_book_id_on_two_platforms_is_not_a_clash(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="SAME", platform="fanqie")
    add_book(db, project="p2", book_id="SAME", platform="qidian")

    assert run(db, tmp_path / "out.json", "fanqie")[0] == 0
    assert run(db, tmp_path / "q.json", "qidian")[0] == 0


def test_two_chapters_with_one_platform_id_in_a_book_are_refused(db, tmp_path) -> None:
    add_book(
        db, project="p1", book_id="B1",
        chapters=((1, "DUP", "published", "甲"), (2, "DUP", "drafted", "乙"), (3, "C3", "published")),
    )
    out = tmp_path / "out.json"

    code, _, stderr = run(db, out)

    assert code == 2 and not out.exists()
    assert "DUP" in stderr and "甲" in stderr and "乙" in stderr


def test_a_chapter_id_held_twice_but_in_a_state_that_is_not_exported_is_no_clash(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "DUP", "published"), (2, "DUP", "unknown")))

    code, _, stderr = run(db, tmp_path / "out.json")

    assert (code, stderr) == (0, "")


def test_the_same_chapter_id_in_two_books_is_not_a_clash(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    add_book(db, project="p2", book_id="B2", chapters=((1, "C1", "published"),))

    assert run(db, tmp_path / "out.json")[0] == 0


def test_the_same_source_ref_twice_is_refused(db, tmp_path) -> None:
    with db.engine.begin() as conn:  # the unique index would stop the second row
        conn.execute(text("DROP INDEX ux_publisher_chapter_bindings_work_number"))
    try:
        add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"), (1, "C2", "drafted")))
        out = tmp_path / "out.json"
        code, _, stderr = run(db, out)
    finally:
        with db.engine.begin() as conn:
            conn.execute(text("TRUNCATE publisher_chapter_bindings, publisher_work_bindings CASCADE"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX ux_publisher_chapter_bindings_work_number "
                    "ON publisher_chapter_bindings (work_binding_id, chapter_number) WHERE chapter_number > 0"
                )
            )

    assert code == 2 and not out.exists()
    assert "project/p1/chapter/1" in stderr


def test_a_chapter_filed_under_another_project_is_refused(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    with db.session_factory.begin() as session:
        session.execute(text("UPDATE publisher_chapter_bindings SET project_id = 'p-other'"))
    out = tmp_path / "out.json"

    code, _, stderr = run(db, out)

    assert code == 2 and not out.exists()
    assert "p-other" in stderr and "p1" in stderr


def test_a_chapter_binding_with_no_project_takes_its_books(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    with db.session_factory.begin() as session:
        session.execute(text("UPDATE publisher_chapter_bindings SET project_id = ''"))

    assert run(db, tmp_path / "out.json")[0] == 0
    assert exported(tmp_path / "out.json")["publications"][0]["source_ref"] == "project/p1/chapter/1"


@pytest.mark.parametrize(
    ("field", "value"),
    [("book", "b" * 513), ("chapter", "c" * 513), ("book_url", "https://x/" + "u" * 2040)],
)
def test_a_value_the_hub_would_refuse_is_refused_here(db, tmp_path, field, value) -> None:
    add_book(
        db, project="p1", book_id=value if field == "book" else "B1",
        url=value if field == "book_url" else "",
        chapters=((1, value if field == "chapter" else "C1", "published"),),
    )
    out = tmp_path / "out.json"

    code, _, stderr = run(db, out)

    assert code == 2 and not out.exists()
    assert "p1" in stderr


def test_an_unwritable_path_is_a_refusal(db, tmp_path) -> None:
    add_book(db, project="p1", book_id="B1")

    code, _, stderr = run(db, tmp_path / "missing-dir" / "out.json")

    assert code == 2
    assert "out.json" in stderr


# --- the same data, the same bytes -----------------------------------------------------------------


def _populate(db: Db, order: list[int]) -> None:
    books = {
        1: dict(project="pa", book_id="B2", name="乙书", chapters=((10, "C10", "published"), (2, "C2", "drafted"),
                                                              (1, "C1", "submitted", "标题"))),
        2: dict(project="pb", book_id="B1", name="甲书", url="https://x.example/B1",
                chapters=((3, "D3", "published"), (1, "D1", "published"))),
        3: dict(project="pc", book_id="B10", chapters=((1, "E1", "drafted"),)),
        4: dict(project="pd", book_id=None, chapters=((1, "", "drafted", "没传上"),)),
    }
    for index in order:
        add_book(db, **books[index])


def test_the_same_data_gives_the_same_bytes_whatever_order_it_was_written_in(db, tmp_path) -> None:
    results = []
    for number, order in enumerate([[1, 2, 3, 4], [4, 3, 2, 1], [2, 4, 1, 3]]):
        with db.engine.begin() as conn:
            conn.execute(text("TRUNCATE publisher_chapter_bindings, publisher_work_bindings CASCADE"))
        _populate(db, order)
        out, again = tmp_path / f"out-{number}.json", tmp_path / f"again-{number}.json"
        assert run(db, out, preferred_client_id="ext-1")[0] == 3
        assert run(db, again, preferred_client_id="ext-1")[0] == 3
        assert out.read_bytes() == again.read_bytes()
        results.append(out.read_bytes())
    assert results[0] == results[1] == results[2]


def test_the_serialisation_is_fixed(db, tmp_path) -> None:
    _populate(db, [1, 2, 3])
    out = tmp_path / "out.json"
    assert run(db, out)[0] == 0

    raw = out.read_bytes()
    document = json.loads(raw)
    assert raw == (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert "乙书".encode() in raw and b"\\u" not in raw
    assert raw.endswith(b"}\n") and not raw.endswith(b"\n\n")
    assert b"\r" not in raw
    # Books by (platform, book id), chapters by book then chapter number: 2 before 10, B1 before B10 before B2.
    assert [d["remote_id"] for d in document["destinations"]] == ["B1", "B10", "B2"]
    assert [p["source_ref"] for p in document["publications"]] == [
        "project/pb/chapter/1", "project/pb/chapter/3",
        "project/pc/chapter/1",
        "project/pa/chapter/1", "project/pa/chapter/2", "project/pa/chapter/10",
    ]


def test_the_export_satisfies_the_hub_parser_whatever_is_in_the_database(db, tmp_path) -> None:
    _populate(db, [1, 2, 3, 4])
    add_book(db, project="p-ü", book_id="B-é", name='"引号"\n换行', chapters=((1, "C-é", "published", "标 题"),))
    out = tmp_path / "out.json"

    assert run(db, out, preferred_client_id="magi3-ext-1")[0] == 3

    document = exported(out)
    assert set(document["destinations"][0]) == {"platform", "remote_id", "remote_url", "settings"}
    assert all(set(p) == {"platform", "destination_remote_id", "source_ref", "remote_id", "remote_url", "official_state"}
               for p in document["publications"])


# --- read-only -------------------------------------------------------------------------------------


def _snapshot(db: Db) -> dict[str, list]:
    with db.session_factory() as session:
        tables = session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        ).scalars().all()
        return {
            table: sorted(map(str, session.execute(text(f'SELECT * FROM "{table}"')).all())) for table in tables
        }


def test_the_export_writes_nothing_to_the_database(db, tmp_path) -> None:
    _populate(db, [1, 2, 3, 4])
    before = _snapshot(db)
    statements: list[str] = []

    @event.listens_for(db.engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    assert run(db, tmp_path / "out.json")[0] == 3

    assert statements
    writes = [s for s in statements if re.match(r"\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE)\b", s, re.I)]
    assert writes == []
    assert _snapshot(db) == before


def test_the_database_refuses_a_write_during_the_export(db, tmp_path, monkeypatch) -> None:
    """The transaction is read-only, so even a bug in the export could not change a row."""
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    reader = export_module._read_bindings  # noqa: SLF001

    def sneaky(session, platform):
        result = reader(session, platform)
        session.execute(text("UPDATE publisher_work_bindings SET book_name = 'changed'"))
        return result

    monkeypatch.setattr(export_module, "_read_bindings", sneaky)

    with pytest.raises(Exception, match="read-only"):
        run(db, tmp_path / "out.json")

    with db.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PublisherWorkBinding).where(
            PublisherWorkBinding.book_name == "changed")) == 0


# --- one snapshot (final review, item 5) -----------------------------------------------------------


def test_both_reads_run_in_one_read_only_repeatable_read_transaction(db, tmp_path, monkeypatch) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    reader = export_module._read_bindings  # noqa: SLF001
    seen: list[tuple[str, str]] = []

    def observed(session, platform):
        seen.append(
            (
                session.execute(text("SHOW transaction_isolation")).scalar_one(),
                session.execute(text("SHOW transaction_read_only")).scalar_one(),
            )
        )
        return reader(session, platform)

    monkeypatch.setattr(export_module, "_read_bindings", observed)

    assert run(db, tmp_path / "out.json")[0] == 0
    assert seen == [("repeatable read", "on")]


def test_a_book_committed_between_the_two_reads_is_wholly_left_out(db, tmp_path) -> None:
    """Under READ COMMITTED the chapter read could see a chapter whose work binding the first read
    had not seen, and it was dropped without a word. One snapshot sees neither."""
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    fired: list[str] = []

    def commit_a_book_after_the_first_read(conn, cursor, statement, parameters, context, executemany):
        if fired or "FROM publisher_work_bindings" not in statement or "JOIN" in statement:
            return
        fired.append(statement)
        add_book(db, project="p2", book_id="B2", chapters=((1, "C9", "published"),))

    event.listen(db.engine, "after_cursor_execute", commit_a_book_after_the_first_read)
    try:
        code, _, stderr = run(db, tmp_path / "out.json")
    finally:
        event.remove(db.engine, "after_cursor_execute", commit_a_book_after_the_first_read)

    assert fired, "the book was committed during the export"
    assert (code, stderr) == (0, "")
    document = exported(tmp_path / "out.json")
    assert [d["remote_id"] for d in document["destinations"]] == ["B1"]
    assert [p["remote_id"] for p in document["publications"]] == ["C1"]
    assert run(db, tmp_path / "again.json")[0] == 0  # the next export carries it
    assert [p["remote_id"] for p in exported(tmp_path / "again.json")["publications"]] == ["C1", "C9"]


def test_a_chapter_whose_work_binding_was_not_read_is_refused_not_dropped() -> None:
    work = export_module._Work(  # noqa: SLF001
        id="w1", project_id="p1", remote_id="B1", remote_url="", name="书"
    )
    chapters = [
        export_module._Chapter(  # noqa: SLF001
            work_id=work_id, project_id="p1", number=number, remote_id=remote_id, remote_url="",
            title=title, state="published",
        )
        for work_id, number, remote_id, title in (("w1", 1, "C1", "一"), ("w-unseen", 2, "C2", "二"))
    ]

    with pytest.raises(export_module.ExportRefused) as refused:
        export_module._plan("fanqie", None, [work], chapters)  # noqa: SLF001

    [problem] = refused.value.problems
    assert "w-unseen" in problem and "chapter 2" in problem and "export again" in problem


# --- the command -----------------------------------------------------------------------------------


def _cli(monkeypatch, db: Db, *argv: str) -> None:
    monkeypatch.setattr("sys.argv", ["forwin", "--database-url", db.url, "publish-hub-export", *argv])
    main()


def test_the_command_is_wired_with_its_options() -> None:
    args = build_parser().parse_args(
        ["publish-hub-export", "--platform", "qidian", "--out", "x.json", "--preferred-client-id", "ext-9"]
    )
    assert (args.command, args.platform, args.out, args.preferred_client_id) == (
        "publish-hub-export", "qidian", "x.json", "ext-9"
    )
    bare = build_parser().parse_args(["publish-hub-export", "--platform", "fanqie", "--out", "x.json"])
    assert bare.preferred_client_id is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--out", "x.json"],
        ["--platform", "fanqie"],
        ["--platform", "weibo", "--out", "x.json"],
        ["--platform", "fanqie", "--out", "x.json", "--preferred-client-id", "  "],
    ],
)
def test_the_command_refuses_bad_arguments(argv) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["publish-hub-export", *argv])
    assert raised.value.code == 2


def test_the_command_exits_0_after_writing(db, tmp_path, monkeypatch, capsys) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"),))
    out = tmp_path / "out.json"

    _cli(monkeypatch, db, "--platform", "fanqie", "--out", str(out), "--preferred-client-id", "ext-1")

    captured = capsys.readouterr()
    assert captured.err == "" and str(out) in captured.out
    assert exported(out)["accounts"][0]["preferred_client_id"] == "ext-1"


def test_the_command_exits_3_after_writing_when_chapters_are_listed(db, tmp_path, monkeypatch, capsys) -> None:
    add_book(db, project="p1", book_id="B1", chapters=((1, "C1", "published"), (2, "", "drafted", "没号")))
    out = tmp_path / "out.json"

    with pytest.raises(SystemExit) as raised:
        _cli(monkeypatch, db, "--platform", "fanqie", "--out", str(out))

    assert raised.value.code == 3
    assert "没号" in capsys.readouterr().err
    assert len(exported(out)["publications"]) == 1


def test_the_command_exits_2_and_writes_nothing_for_a_shared_book(db, tmp_path, monkeypatch, capsys) -> None:
    add_book(db, project="p1", book_id="SAME")
    add_book(db, project="p2", book_id="SAME")
    out = tmp_path / "out.json"

    with pytest.raises(SystemExit) as raised:
        _cli(monkeypatch, db, "--platform", "fanqie", "--out", str(out))

    assert raised.value.code == 2
    assert not out.exists() and "p1" in capsys.readouterr().err
