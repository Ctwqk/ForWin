from __future__ import annotations

from forwin.canon_quality.identity import analyze_identity_roles


def test_central_relative_drift_blocks_without_bridge() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=31,
        draft_id="d1",
        body="陆明终于确认，林远是他的祖父。",
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
        body="陆明终于确认，此前父亲身份是伪装，林远其实是他的祖父。",
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


def test_gender_marker_drift_blocks_without_bridge() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=12,
        draft_id="d1",
        body="陆明得知，韩青是自己叔叔。这个自称是他叔叔的男人把钥匙交给他。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert any(fact.character_name == "韩青" and fact.payload.get("gender_label") == "male" for fact in facts)
    assert any(signal.signal_type == "identity_gender_conflict" and signal.severity == "error" for signal in signals)


def test_gender_marker_does_not_assign_later_protagonist_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=20,
        draft_id="d1",
        body="陆明扶着韩青从竖井爬出。两人身上都是铁锈，陆明低声说他不能在这里停下。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_quoted_speech_pronoun_to_speaker() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=22,
        draft_id="d1",
        body="韩青的手指微微颤抖，“周砚的真正目的不是维护秩序，他是想抹除反对者。”",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_pushed_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=33,
        draft_id="d1",
        body="韩青推开他，站到扫描仪前，把自己的左眼对准镜头。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_looked_at_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=33,
        draft_id="d1",
        body="韩青回头看了他一眼。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_comparison_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=33,
        draft_id="d33",
        body="韩青比他早到几秒，正蹲在墙角查看父亲留下的金属盒。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_gaze_and_voice_object_pronouns_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=33,
        draft_id="d33",
        body=(
            "“是为了保护旧城的真相。”韩青直视着他的眼睛。"
            "韩青抬头看他，声音平稳，但额角的汗珠出卖了她的紧张。"
            "“还有多久？”韩青的声音把他拉回现实。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_forgotten_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=35,
        draft_id="d35",
        body="韩青会活下来，但会忘记一切——忘记他是谁，忘记他们之间的每一次对话。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_quoted_vocative_speaker_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=24,
        draft_id="d24",
        body="“陆明。”她压低声音，“你确定是这层？”陆明推开门。",
        previous_facts=[
            {
                "character_name": "陆明",
                "role_label": "gender:male",
                "truth_value": "true",
                "chapter_number": 1,
                "payload": {"gender_label": "male"},
            }
        ],
        central_characters={"陆明"},
    )

    assert not [fact for fact in facts if fact.character_name == "陆明" and fact.payload.get("gender_label") == "female"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d1",
        body="韩青已经走到床前，将一个便携终端递到他面前。屏幕上的数据让陆明的呼吸骤然一紧。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_protected_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d1",
        body="韩青冒着暴露身份的风险跟进来，只是为了不让他在昏迷中被核心系统直接处理掉。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_body_part_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d1",
        body="韩青猛地熄灭应急灯。黑暗里，陆明感觉到韩青的手按在他手腕上，力道很紧。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_interrupted_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d1",
        body="韩青打断他，把手环递到他眼前，低声说核心系统出现异常波动。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_ignores_other_character_pronoun_after_named_voice_intro() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青的声音从右侧传来，压得很低：“别动。”"
            "陆明侧过头。韩青打断他，语气里带着罕见的紧迫。"
            "她把终端屏幕转过来。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_locative_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青蹲在他身侧，核心系统制服的袖口卷到小臂。"
            "她把终端屏幕转向陆明，语气很低。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_facing_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "陆明侧过头，看见韩青站在拘押室中央，背对着他，双手微微张开，挡在他和门口之间。"
            "她穿着深灰色风衣，语气平静。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_gaze_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青盯着他：“但这意味着我们要穿过核心系统底层。”"
            "她转身看向走廊，压低声音。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_covering_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="陆明在核心系统底层苏醒，发现记忆重置周期仅剩85分钟；他与韩青通过通风管道逃脱，韩青为掩护他中弹失联。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_supporting_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="韩青架着他从巷道拐角闪出。她压低声音，拖着他往旧轨入口移动。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_noticed_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="“怎么了？”韩青察觉到他的异常。她停下脚步，看向墙上的陆氏暗记。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_looked_toward_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="韩青沉默了几秒，然后抬头看向他。“我去取。”",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_cutoff_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="“没有如果。”韩青截断他的话，转身朝岔道的方向走去。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_thrown_to_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="韩青从腰后抽出第二把短管武器，检查弹夹，然后丢给他。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_helped_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=24,
        draft_id="d24",
        body="陆明走过去帮她撬开瓷砖，露出一个约三十厘米深的暗格。",
        previous_facts=[
            {
                "character_name": "陆明",
                "role_label": "gender:male",
                "truth_value": "true",
                "chapter_number": 1,
                "payload": {"gender_label": "male"},
            }
        ],
        central_characters={"陆明"},
    )

    assert not [fact for fact in facts if fact.character_name == "陆明" and fact.payload.get("gender_label") == "female"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_held_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=24,
        draft_id="d24",
        body="韩青伸手想拿那张纸条，陆明按住了她的手背。",
        previous_facts=[
            {
                "character_name": "陆明",
                "role_label": "gender:male",
                "truth_value": "true",
                "chapter_number": 1,
                "payload": {"gender_label": "male"},
            }
        ],
        central_characters={"陆明"},
    )

    assert not [fact for fact in facts if fact.character_name == "陆明" and fact.payload.get("gender_label") == "female"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_followed_body_part_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=24,
        draft_id="d24",
        body="陆明顺着她的手指看过去。日志的加密前缀清晰浮现。",
        previous_facts=[
            {
                "character_name": "陆明",
                "role_label": "gender:male",
                "truth_value": "true",
                "chapter_number": 1,
                "payload": {"gender_label": "male"},
            }
        ],
        central_characters={"陆明"},
    )

    assert not [fact for fact in facts if fact.character_name == "陆明" and fact.payload.get("gender_label") == "female"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_ignores_generic_artifact_name_from_previous_facts() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="如果周砚知道你手里有这张照片，她会不惜一切代价让你永远留在那里。",
        previous_facts=[
            {
                "character_name": "照片",
                "role_label": "gender:male",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "male"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "照片"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_uses_next_sentence_pronoun_after_name_sentence_end() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=22,
        draft_id="d1",
        body=(
            "中央的石柱旁站着两个穿深色夹克的男人。然后他看见了韩青。"
            "她坐在大厅东侧的铁椅上。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 8,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert any(fact.character_name == "韩青" and fact.payload.get("gender_label") == "female" for fact in facts)
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_thinker_pronoun_to_named_memory_object() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="韩青。他默念这个名字，但她的眼睛是什么颜色，已经开始变得模糊。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_object_pronoun_inside_named_voice_description() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "韩青的声音——那个在钟塔里告诉他真相的声音——正在从他的记忆里被剥离。"
            "他记得她说过的每一个字。"
        ),
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_stops_at_semicolon_after_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="陆明确认韩青被关押在地下检修线第三层；他利用临时权限绕过封锁。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_bind_new_clause_pronoun_after_rescue_object() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="他必须在倒计时结束之前救出韩青，然后——他看了一眼腕间跳动的数字。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_bind_accompanying_object_pronoun() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="韩青被抓，是因为陪他去了钟塔。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_bind_dative_speech_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="韩青在钟塔对他说过的一句话已经变得模糊，但她留下的警告仍在。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_bind_given_code_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="韩青给过他这段代码，但她当时只匆匆说了一遍。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_bind_new_subject_after_captured_state() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body="韩青已经被抓了，他一个人去能做什么？陆明只能先保住后门芯片。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 28,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_treat_plural_they_as_named_character_male() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body="陆明看向屏幕里的韩青，想起他们在钟塔的会面，想起她说过的每一句话。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 28,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_inner_monologue_pronoun_after_name_memory() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="韩青。\n\n他在心里默念这个名字，但她的轮廓已经开始变得模糊。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 25,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_seen_object_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=34,
        draft_id="d34",
        body="韩青坐在靠墙的金属椅上，手腕被磁力束带扣在扶手上，抬头看见他的表情没有惊讶，只有一种疲惫。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 33,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_pushed_object_pronoun_with_aspect_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="韩青用右手推了他一把，力道不大，却让陆明撞上身后的铁门。她低声说：别废话。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 22,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]


def test_gender_marker_does_not_assign_recalled_quoted_pronoun_to_named_character() -> None:
    signals, facts = analyze_identity_roles(
        project_id="p1",
        chapter_number=35,
        draft_id="d35",
        body="陆明想起韩青在羁押室里说的那句话——你父亲不是在隐瞒，他是在保护。她当时眼神清明。",
        previous_facts=[
            {
                "character_name": "韩青",
                "role_label": "gender:female",
                "truth_value": "true",
                "chapter_number": 34,
                "payload": {"gender_label": "female"},
            }
        ],
        central_characters={"韩青"},
    )

    assert not [fact for fact in facts if fact.character_name == "韩青" and fact.payload.get("gender_label") == "male"]
    assert not [signal for signal in signals if signal.signal_type == "identity_gender_conflict"]
