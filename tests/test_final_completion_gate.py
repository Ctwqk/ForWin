from __future__ import annotations

from forwin.canon_quality.final_completion import analyze_final_completion


def test_final_chapter_blocks_open_main_crisis_hook() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        body=(
            "陆明发现核心系统仍在运转，系统巡检员包围档案室并引爆炸弹。"
            "他从暗道逃生，前方传来未知机械运转声，暗道的尽头有什么东西在等待着他。"
        ),
    )

    assert any(signal.signal_type == "final_hook_unresolved" for signal in signals)
    assert signals[0].severity == "error"
    assert "repair_hint" in signals[0].payload


def test_final_chapter_blocks_trapped_ending_from_summary_and_body() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明读取父亲封存的记忆数据，但记忆芯片损坏，他被困在封闭的第五层。",
        body=(
            "父亲的投影突然剧烈闪烁。闸门正在缓缓关闭，井沿的数据接口处冒出青烟，"
            "记忆芯片开始过热。芯片边缘断裂了一半。最后一丝光线被切断，"
            "地下检修线第五层陷入了彻底的黑暗。"
        ),
    )

    assert any(signal.signal_type == "final_hook_unresolved" for signal in signals)
    assert signals[0].severity == "error"
    assert "芯片损坏" in signals[0].description or "被困" in signals[0].description


def test_final_chapter_without_main_crisis_resolution_is_blocked() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明终于确认核心系统是记忆控制装置。",
        body="陆明确认了核心系统和记忆重置的真相，把父亲的记录藏进衣袋，走向旧轨深处。",
    )

    assert any(signal.signal_type == "final_resolution_missing" for signal in signals)
    assert signals[0].severity == "error"


def test_final_chapter_blocks_intended_truth_reveal_with_new_pursuit_hook() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明带着父亲留下的纸质档案逃到中央广场，决定当众公开真相，而系统巡检员的装甲车已抵达广场入口。",
        body=(
            "陆明把档案举过头顶，说核心系统帮你们忘了。"
            "远处，广场入口处，三辆白色装甲车正在减速驶近。人群开始骚动。"
            "陆明深吸一口气，念出了档案上第一行字。"
        ),
    )

    assert any(signal.signal_type == "final_hook_unresolved" for signal in signals)
    assert signals[0].severity == "error"


def test_final_chapter_blocks_pending_resolution_even_without_pursuit_keyword() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明决定公开真相。",
        body="陆明站上喷泉池边，准备把核心系统的证据公开给全城，开始念出档案上的第一段记录。",
    )

    assert any(signal.signal_type == "final_resolution_pending" for signal in signals)
    assert signals[0].severity == "error"


def test_final_chapter_blocks_attempted_system_shutdown_without_completion() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明准备关闭核心系统。",
        body="陆明把钥匙插进核心接口，试图关闭核心系统，重置倒计时仍在屏幕上跳动。",
    )

    assert any(signal.signal_type == "final_resolution_pending" for signal in signals)
    assert signals[0].severity == "error"


def test_final_chapter_blocks_live_open_pursuit_and_locked_door_ending() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary=(
            "陆明公开揭露核心系统后逃入下水道深处，发现父亲留下的笔记，"
            "得知遗忘之井的线索和不要相信任何人的警告。"
        ),
        body=(
            "地图边缘写着：真正的档案不在核心系统，不在公会，在遗忘之井。"
            "陆明推门，铁门纹丝不动。他低头看了看手里的半截钥匙，"
            "钥匙的齿形和锁孔完全吻合，但缺少了最关键的那一片。"
            "他靠在门上，闭上眼睛。身后，脚步声越来越近。"
        ),
    )

    assert any(signal.signal_type == "final_hook_unresolved" for signal in signals)
    assert signals[0].severity == "error"


def test_final_chapter_allows_explicit_main_crisis_resolution() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="尾声",
        summary="核心系统关闭，记忆重置停止。",
        body=(
            "陆明把证据公开给全城，核心系统关闭，记忆重置停止。"
            "尾声里，他听见远处旧机器重新运转，但这只是修复城市档案的新工程。"
        ),
    )

    assert signals == []


def test_final_chapter_allows_light_sequel_hint_after_main_crisis_resolution() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="尾声",
        summary="核心系统关闭，记忆重置停止。",
        body=(
            "陆明把证据公开给全城，核心系统关闭，记忆重置停止。"
            "数周后，他在整理废墟档案时发现一条新线索，但这已经不是这次重置危机。"
        ),
    )

    assert signals == []


def test_final_chapter_allows_aftermath_door_closing_after_explicit_resolution() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明用芯片阻止记忆重置，核心系统已关闭，旧城进入新秩序。",
        body=(
            "陆明将芯片插入控制台，屏幕显示：核心系统主控程序已终止。记忆重置已取消。"
            "旧城居民已经看见真相，公共档案系统进入离线模式。"
            "两人离开档案室，身后铁门缓缓关闭。核心系统顶端的光环慢慢熄灭。"
        ),
    )

    assert signals == []


def test_final_chapter_allows_pre_resolution_danger_when_finale_resolves_afterward() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="陆明公开真相，韩青终止记忆重置程序。",
        body=(
            "广播室的门被撞开，陆明被击中后倒下。"
            "韩青输入最后一段代码，核心系统顶端的红光熄灭，重置程序终止了。"
            "夜色中，旧城的记忆没有被抹去，真相公开了。"
        ),
    )

    assert signals == []


def test_final_chapter_allows_completed_shutdown_after_entering_core_location() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary=(
            "陆明进入地下检修线第五层，利用父亲留下的密钥成功关闭核心系统记忆系统，"
            "最终所有被抹除的记录公开，旧城记忆恢复。"
        ),
        body=(
            "陆明进入地下检修线第五层，打开父亲留下的终端。"
            "他按下确认键，屏幕显示：【系统关闭完成。所有存档记录已公开。】"
            "核心系统的警报声戛然而止，公共终端重新亮起。"
            "旧城的记忆，回来了。"
        ),
    )

    assert signals == []


def test_final_chapter_blocks_post_resolution_handoff_task() -> None:
    signals = analyze_final_completion(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        is_final_chapter=True,
        title="倒计时：最后一日",
        summary="核心系统记忆重置系统失效，旧城历史公开。",
        body=(
            "陆明按下红色按钮，将陆远舟留下的记录广播给全城。"
            "从这一刻起，核心系统的记忆重置系统已经失效。"
            "韩青扶住他，说：我们走，去档案署，把最后一段记忆记录交给所有人。"
            "旧城将不再有记忆重置，每个人都将拥有自己的历史。"
        ),
    )

    assert any(signal.signal_type == "final_hook_unresolved" for signal in signals)
    assert "最后一段记忆记录" in signals[0].description or "去档案署" in signals[0].description
