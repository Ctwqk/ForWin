from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from forwin.canon.outbox_events import CanonPublisherBindingSnapshot
from forwin.models.publisher import PublisherCoverAsset, PublisherUploadJob
from forwin.publish_hub.payloads import (
    binding_snapshot_sha256,
    chapter_body,
    cover_body,
    create_body,
)
from forwin.publish_hub.refs import (
    chapter_key,
    cover_key,
    cover_source_ref_for,
    create_key,
    source_ref_for,
)

DESTINATION = "3b3c8f1e-0000-4000-8000-000000000002"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _chapter_job(*, publish: bool = True, candidate_id: str = "cand-1") -> PublisherUploadJob:
    return PublisherUploadJob(
        id="job-1",
        project_id="p1",
        candidate_id=candidate_id,
        chapter_number=3,
        platform_id="fanqie",
        task_kind="chapter_upload",
        book_name="雾港",
        chapter_title="第三章 旧城回声",
        body_text="雨停以后，旧城的钟声沿着空巷传来。\n第二段。",
        publish=publish,
        result_payload_json=json.dumps(
            {"canon_identity": {"canon_idempotency_key": "canon-key-1"}}
        ),
    )


def test_chapter_body_has_the_exact_hub_request_shape() -> None:
    job = _chapter_job()

    assert chapter_body(job, DESTINATION) == {
        "idempotency_key": chapter_key(job),
        "destination_id": DESTINATION,
        "source_ref": "project/p1/chapter/3",
        "content_kind": "novel_chapter",
        "action": "upload",
        "payload_version": 1,
        "payload": {
            "book_name": "雾港",
            "chapter_title": "第三章 旧城回声",
            "body_text": "雨停以后，旧城的钟声沿着空巷传来。\n第二段。",
            "mode": "publish",
        },
        "content_refs": [],
    }


@pytest.mark.parametrize(("publish", "mode"), [(True, "publish"), (False, "draft")])
def test_chapter_body_mode_follows_the_job_publish_flag(publish, mode) -> None:
    body = chapter_body(_chapter_job(publish=publish), DESTINATION)

    assert body["payload"]["mode"] == mode


def test_chapter_body_source_ref_ignores_the_candidate() -> None:
    first = chapter_body(_chapter_job(candidate_id="cand-1"), DESTINATION)
    regenerated = chapter_body(_chapter_job(candidate_id="cand-2"), DESTINATION)

    assert first["source_ref"] == regenerated["source_ref"] == source_ref_for("p1", 3)
    assert first["idempotency_key"] != regenerated["idempotency_key"]


def test_chapter_body_refuses_other_job_kinds_and_missing_destinations() -> None:
    cover = _chapter_job()
    cover.task_kind = "cover_upload"
    with pytest.raises(ValueError, match="chapter_upload"):
        chapter_body(cover, DESTINATION)
    with pytest.raises(ValueError, match="destination"):
        chapter_body(_chapter_job(), "")


BOOK_META = {
    "audience": "male",
    "primary_category": "悬疑灵异",
    "theme_tags": ["都市", "悬疑"],
    "role_tags": ["刑警"],
    "plot_tags": ["旧案", "港区", "多余的第三个"],
    "protagonist_names": ["韩砚", "苏晚", "第三个人"],
    "intro": "  一桩旧案重启，港区的谜团层层展开。  ",
}


def _binding(**overrides) -> dict:
    binding = {
        "platform": "fanqie",
        "book_name": "雾港",
        "upload_url": "https://fanqienovel.com/main/writer/book-manage",
        "create_if_missing": True,
        "cover_generation_enabled": True,
        "book_meta": BOOK_META,
        "route": "hub",
        "hub_destination_id": DESTINATION,
    }
    binding.update(overrides)
    return binding


def test_create_body_maps_the_existing_book_meta_into_the_hub_schema() -> None:
    body = create_body(_binding(), DESTINATION, "p1")

    snapshot_sha = binding_snapshot_sha256(_binding(), DESTINATION)
    assert body == {
        "idempotency_key": create_key("p1", "fanqie", snapshot_sha),
        "destination_id": DESTINATION,
        "action": "create_destination",
        "payload_version": 1,
        "payload": {
            "book_meta": {
                "audience": "male",
                "category": "悬疑灵异",
                "tags": [
                    "theme:都市",
                    "theme:悬疑",
                    "role:刑警",
                    "plot:旧案",
                    "plot:港区",
                ],
                "protagonist_names": ["韩砚", "苏晚"],
                "intro": "一桩旧案重启，港区的谜团层层展开。",
            }
        },
        "content_refs": [],
    }
    assert "source_ref" not in body and "content_kind" not in body


def test_create_body_payload_carries_only_the_five_schema_fields() -> None:
    meta = create_body(_binding(), DESTINATION, "p1")["payload"]["book_meta"]

    assert set(meta) == {"audience", "category", "tags", "protagonist_names", "intro"}
    assert set(create_body(_binding(), DESTINATION, "p1")["payload"]) == {"book_meta"}


def test_create_body_fills_every_required_field_when_the_meta_is_empty() -> None:
    for binding in (_binding(book_meta={}), _binding(book_meta=None)):
        meta = create_body(binding, DESTINATION, "p1")["payload"]["book_meta"]
        assert meta == {
            "audience": "",
            "category": "",
            "tags": [],
            "protagonist_names": [],
            "intro": "",
        }


def test_create_body_accepts_the_canon_snapshot_model_and_the_settings_model() -> None:
    from forwin.api_schema.project import ProjectAutomationPublishSettings

    snapshot = CanonPublisherBindingSnapshot.model_validate(
        {k: v for k, v in _binding().items()}
    )
    settings = ProjectAutomationPublishSettings.model_validate(_binding())
    expected = create_body(_binding(), DESTINATION, "p1")

    assert create_body(snapshot, DESTINATION, "p1") == expected
    assert create_body(settings, DESTINATION, "p1") == expected


def test_create_body_key_changes_only_when_what_is_created_changes() -> None:
    base = create_body(_binding(), DESTINATION, "p1")["idempotency_key"]
    assert create_body(_binding(), "another-destination", "p1")["idempotency_key"] != base

    other_route = _binding(route="legacy", hub_destination_id=None, upload_url="https://x/")
    assert create_body(other_route, DESTINATION, "p1")["idempotency_key"] == base
    # What counts is the destination the request is sent to, not the one the binding names.
    stale_destination = _binding(hub_destination_id="stale-destination")
    assert create_body(stale_destination, DESTINATION, "p1")["idempotency_key"] == base
    changed_intro = _binding(book_meta={**BOOK_META, "intro": "另一个简介"})
    assert create_body(changed_intro, DESTINATION, "p1")["idempotency_key"] != base
    assert create_body(_binding(), DESTINATION, "p2")["idempotency_key"] != base
    assert (
        create_body(_binding(platform="qidian"), DESTINATION, "p1")["idempotency_key"]
        != base
    )
    assert base.startswith("forwin:create:p1:fanqie:")


def test_create_body_requires_platform_project_and_destination() -> None:
    with pytest.raises(ValueError, match="platform"):
        create_body(_binding(platform=""), DESTINATION, "p1")
    with pytest.raises(ValueError, match="project"):
        create_body(_binding(), DESTINATION, "")
    with pytest.raises(ValueError, match="destination"):
        create_body(_binding(), "", "p1")


def _cover_asset(path, *, asset_id: str = "asset-1") -> PublisherCoverAsset:
    return PublisherCoverAsset(
        id=asset_id,
        project_id="p1",
        file_path=str(path),
        mime_type="image/png",
        selection_state="selected",
    )


def test_create_body_has_no_content_ref_without_a_cover() -> None:
    assert create_body(_binding(), DESTINATION, "p1")["content_refs"] == []
    assert create_body(_binding(), DESTINATION, "p1", cover=None)["content_refs"] == []


def test_create_body_adds_one_cover_ref_for_a_selected_cover_file(tmp_path) -> None:
    cover_file = tmp_path / "asset-1.png"
    cover_file.write_bytes(PNG_1X1)

    body = create_body(_binding(), DESTINATION, "p1", cover=_cover_asset(cover_file))

    assert body["content_refs"] == [
        {
            "uri": "file://" + str(cover_file),
            "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
            "size": len(PNG_1X1),
            "mime": "image/png",
            "role": "cover",
        }
    ]


def test_create_body_refuses_a_cover_whose_file_is_missing(tmp_path) -> None:
    with pytest.raises(ValueError, match="cover"):
        create_body(
            _binding(), DESTINATION, "p1", cover=_cover_asset(tmp_path / "gone.png")
        )


def _cover_upload_job(path, content: bytes = PNG_1X1, *, asset_id: str = "asset-1"):
    return PublisherUploadJob(
        id="cover-job-1",
        project_id="p1",
        platform_id="qidian",
        task_kind="cover_upload",
        body_sha256=hashlib.sha256(content).hexdigest(),
        result_payload_json=json.dumps(
            {
                "project_id": "p1",
                "work_binding_id": "work-1",
                "cover_asset_id": asset_id,
                "file_path": str(path),
            }
        ),
    )


def test_cover_body_has_the_exact_hub_request_shape(tmp_path) -> None:
    cover_file = tmp_path / "asset-1.png"
    cover_file.write_bytes(PNG_1X1)
    job = _cover_upload_job(cover_file)

    assert cover_body(job, DESTINATION) == {
        "idempotency_key": cover_key(job),
        "destination_id": DESTINATION,
        "source_ref": "project/p1/cover/asset-1",
        "content_kind": "novel_cover",
        "action": "upload_cover",
        "payload_version": 1,
        "payload": {},
        "content_refs": [
            {
                "uri": "file://" + str(cover_file),
                "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
                "size": len(PNG_1X1),
                "mime": "image/png",
                "role": "cover",
            }
        ],
    }


def test_cover_body_uri_is_the_path_the_legacy_job_hands_the_extension(tmp_path) -> None:
    cover_file = tmp_path / "asset-1.png"
    cover_file.write_bytes(PNG_1X1)
    job = _cover_upload_job(cover_file)
    legacy_file_path = json.loads(job.result_payload_json)["file_path"]

    [ref] = cover_body(job, DESTINATION)["content_refs"]

    assert ref["uri"] == "file://" + legacy_file_path
    assert ref["uri"].startswith("file:///")


def test_cover_body_percent_encodes_paths_that_are_not_valid_in_a_uri(tmp_path) -> None:
    folder = tmp_path / "my covers"
    folder.mkdir()
    cover_file = folder / "封面 1.png"
    cover_file.write_bytes(PNG_1X1)

    [ref] = cover_body(_cover_upload_job(cover_file), DESTINATION)["content_refs"]

    assert " " not in ref["uri"] and ref["uri"].isascii()
    assert ref["uri"].endswith("/my%20covers/%E5%B0%81%E9%9D%A2%201.png")


def test_cover_body_detects_a_jpeg(tmp_path) -> None:
    cover_file = tmp_path / "asset-1.jpg"
    cover_file.write_bytes(JPEG_BYTES)

    [ref] = cover_body(_cover_upload_job(cover_file, JPEG_BYTES), DESTINATION)[
        "content_refs"
    ]

    assert ref["mime"] == "image/jpeg" and ref["size"] == len(JPEG_BYTES)


def test_cover_body_verifies_the_file_hash_before_building(tmp_path) -> None:
    cover_file = tmp_path / "asset-1.png"
    cover_file.write_bytes(PNG_1X1)
    job = _cover_upload_job(cover_file)

    cover_file.write_bytes(PNG_1X1 + b"tampered")

    with pytest.raises(ValueError, match="sha256"):
        cover_body(job, DESTINATION)


def test_cover_body_refuses_a_missing_or_empty_file(tmp_path) -> None:
    with pytest.raises(ValueError, match="cover file"):
        cover_body(_cover_upload_job(tmp_path / "gone.png"), DESTINATION)

    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="cover file"):
        cover_body(_cover_upload_job(empty, b""), DESTINATION)


def test_cover_body_refuses_a_relative_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        cover_body(_cover_upload_job("covers/asset-1.png"), DESTINATION)


def test_cover_body_refuses_other_job_kinds(tmp_path) -> None:
    with pytest.raises(ValueError, match="cover_upload"):
        cover_body(_chapter_job(), DESTINATION)


def test_two_cover_assets_get_two_source_refs_and_one_asset_gets_one(tmp_path) -> None:
    first_file = tmp_path / "asset-1.png"
    first_file.write_bytes(PNG_1X1)
    second_file = tmp_path / "asset-2.png"
    second_file.write_bytes(JPEG_BYTES)

    first = cover_body(_cover_upload_job(first_file, asset_id="asset-1"), DESTINATION)
    again = cover_body(_cover_upload_job(first_file, asset_id="asset-1"), DESTINATION)
    second = cover_body(
        _cover_upload_job(second_file, JPEG_BYTES, asset_id="asset-2"), DESTINATION
    )

    assert first["source_ref"] == again["source_ref"] == cover_source_ref_for("p1", "asset-1")
    assert second["source_ref"] == cover_source_ref_for("p1", "asset-2")
    assert first["source_ref"] != second["source_ref"]
    assert first["source_ref"] != source_ref_for("p1", 1)


def test_builders_return_plain_json_serialisable_dicts(tmp_path) -> None:
    cover_file = tmp_path / "asset-1.png"
    cover_file.write_bytes(PNG_1X1)
    bodies = [
        chapter_body(_chapter_job(), DESTINATION),
        create_body(_binding(), DESTINATION, "p1", cover=_cover_asset(cover_file)),
        cover_body(_cover_upload_job(cover_file), DESTINATION),
    ]

    for body in bodies:
        assert json.loads(json.dumps(body)) == body
        assert isinstance(body, dict)
    # create_body is given an ORM-free namespace-like cover too.
    namespace_cover = SimpleNamespace(file_path=str(cover_file), id="asset-1")
    assert create_body(_binding(), DESTINATION, "p1", cover=namespace_cover)["content_refs"]


def test_repointing_a_binding_to_another_destination_gives_another_create_key() -> None:
    first = create_body(_binding(), DESTINATION, "p1")
    second_destination = "9d1e5a70-0000-4000-8000-000000000009"
    second = create_body(_binding(hub_destination_id=second_destination), second_destination, "p1")

    assert first["payload"] == second["payload"], "same meta"
    assert first["destination_id"] != second["destination_id"]
    assert first["idempotency_key"] != second["idempotency_key"]
    assert binding_snapshot_sha256(_binding(), DESTINATION) != binding_snapshot_sha256(
        _binding(), second_destination
    )
    with pytest.raises(ValueError, match="destination"):
        binding_snapshot_sha256(_binding(), "")


def test_fanqie_tags_carry_their_group_in_front_of_the_tag() -> None:
    """book_meta.tags is a flat list; the group rides in the string: "<group>:<tag>".

    The group is theme, role or plot, and the reader splits on the first ":" only, so a tag may
    itself contain ":". The order is theme, role, plot, each in the order ForWin keeps them.
    """
    binding = _binding(
        platform="fanqie",
        book_meta={
            **BOOK_META,
            "theme_tags": ["末世:重生", "都市"],
            "role_tags": ["刑警"],
            "plot_tags": ["theme:像分组的标签", "旧案"],
        },
    )

    tags = create_body(binding, DESTINATION, "p1")["payload"]["book_meta"]["tags"]

    assert tags == [
        "theme:末世:重生",
        "theme:都市",
        "role:刑警",
        "plot:theme:像分组的标签",
        "plot:旧案",
    ]
    decoded = [tuple(tag.split(":", 1)) for tag in tags]
    assert decoded[0] == ("theme", "末世:重生")
    assert decoded[3] == ("plot", "theme:像分组的标签")
    assert {group for group, _ in decoded} == {"theme", "role", "plot"}


def test_fanqie_groups_survive_with_one_group_empty_and_the_two_tag_limit_per_group() -> None:
    binding = _binding(
        book_meta={
            **BOOK_META,
            "theme_tags": [],
            "role_tags": ["a", "b", "c"],
            "plot_tags": [" ", "x"],
        }
    )

    tags = create_body(binding, DESTINATION, "p1")["payload"]["book_meta"]["tags"]

    assert tags == ["role:a", "role:b", "plot:x"]


def test_qidian_without_tags_has_an_empty_tag_list_and_keeps_the_rest() -> None:
    qidian_meta = {
        "audience": "male",
        "primary_category": "悬疑",
        "intro": "一桩旧事重启，现实与悬念交织。",
    }

    body = create_body(
        _binding(platform="qidian", book_meta=qidian_meta), DESTINATION, "p1"
    )

    assert body["payload"]["book_meta"] == {
        "audience": "male",
        "category": "悬疑",
        "tags": [],
        "protagonist_names": [],
        "intro": "一桩旧事重启，现实与悬念交织。",
    }
    assert body["idempotency_key"].startswith("forwin:create:p1:qidian:")


def test_a_qidian_binding_that_does_have_tags_gets_the_same_encoding() -> None:
    body = create_body(
        _binding(platform="qidian", book_meta={"plot_tags": ["旧案"]}), DESTINATION, "p1"
    )

    assert body["payload"]["book_meta"]["tags"] == ["plot:旧案"]
