from __future__ import annotations

import inspect

from forwin.book_genesis_core import materialize


def test_materialize_wrappers_do_not_keep_unreachable_legacy_bodies() -> None:
    book_arcs_source = inspect.getsource(materialize.materialize_book_arcs)
    chapter_plans_source = inspect.getsource(materialize.materialize_arc_chapter_plans)

    assert "pack = self.load_pack(revision)" not in book_arcs_source
    assert "pack = self.load_pack(revision)" not in chapter_plans_source
    assert "return self.handoff.arc_materializer.materialize_book_arcs" in book_arcs_source
    assert (
        "return self.handoff.chapter_materializer.materialize_arc_chapter_plans"
        in chapter_plans_source
    )
