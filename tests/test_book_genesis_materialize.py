from __future__ import annotations

import inspect

from forwin.genesis import materialize


def test_materialize_wrappers_do_not_keep_unreachable_legacy_bodies() -> None:
    book_arcs_source = inspect.getsource(materialize.materialize_book_arcs)
    assert "pack = self.load_pack(revision)" not in book_arcs_source
    assert "return self.handoff.arc_materializer.materialize_book_arcs" in book_arcs_source
    assert not hasattr(materialize, "materialize_arc_chapter_plans")
    assert not hasattr(materialize, "promote_next_arc_if_needed")
