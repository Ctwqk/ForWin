"""Structural Genesis instructions for genre-defined world variables."""

from forwin.book_state.schema import (
    WORLD_VARIABLE_SOURCE_KINDS, WORLD_VARIABLE_TENDENCIES, WORLD_VARIABLE_VISIBILITY,
)

WORLD_PROFILE_SECTION_TITLE = "制度与资源经济"
WORLD_VARIABLE_OUTPUT_REQUIREMENTS = (
    "world_variables 每项为 {id, label, source_kind, source_profile_id, scale, level, scope_refs, holder_refs, visibility, drift_tendency, summary}；",
    f"source_kind 取 {' / '.join(WORLD_VARIABLE_SOURCE_KINDS)}，source_profile_id 引用对应制度或资源经济 profile 的 id；",
    "每个 profile 至多一个变量，scale 为 2–7 个按本书题材自拟且不重复的有序档位，level 为从 0 起的当前档位下标；",
    "scope_refs 只引用地图节点或行动者，holder_refs 只引用行动者；",
    f"visibility 取 {' / '.join(WORLD_VARIABLE_VISIBILITY)}，drift_tendency 取 {' / '.join(WORLD_VARIABLE_TENDENCIES)}。",
)
