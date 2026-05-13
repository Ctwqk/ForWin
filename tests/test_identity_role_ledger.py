from __future__ import annotations

from forwin.canon_quality.identity import analyze_identity_roles


def test_central_relative_drift_blocks_without_bridge() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=31,
        draft_id="d1",
        body="林澈终于确认，林远是他的祖父。",
        previous_facts=[
            {
                "character_name": "林远",
                "relationship_to_protagonist": "父亲",
                "truth_value": "true",
                "chapter_number": 6,
            }
        ],
        central_characters={"林远"},
    )

    assert facts[0].relationship_to_protagonist == "祖父"
    assert any(signal.signal_type == "identity_relationship_conflict" and signal.severity == "error" for signal in signals)


def test_identity_drift_with_lie_bridge_is_warning() -> None:
    signals, _facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=31,
        draft_id="d1",
        body="林澈终于确认，此前父亲身份是伪装，林远其实是他的祖父。",
        previous_facts=[
            {
                "character_name": "林远",
                "relationship_to_protagonist": "父亲",
                "truth_value": "true",
                "chapter_number": 6,
            }
        ],
        central_characters={"林远"},
    )

    assert not [signal for signal in signals if signal.severity == "error"]
    assert any(signal.signal_type == "identity_relationship_bridge" for signal in signals)
