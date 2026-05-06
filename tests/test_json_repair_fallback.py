from __future__ import annotations

import unittest

from forwin.utils import parse_llm_json


class JsonRepairFallbackTests(unittest.TestCase):
    def test_parse_llm_json_recovers_text_field_with_unescaped_quotes(self) -> None:
        raw = """```json
{
  "text": "雨声像千军万马碾压过屋檐。\n\n上面只有三行字，是陈老七临死前塞进我掌心的——识别"裂口"的方法。"
}
```"""

        parsed = parse_llm_json(raw, error_prefix="test")

        self.assertIn("text", parsed)
        self.assertIn("body", parsed)
        self.assertEqual(parsed["text"], parsed["body"])
        self.assertIn('识别"裂口"的方法', parsed["text"])

    def test_parse_llm_json_preserves_smart_quotes_inside_valid_json_strings(self) -> None:
        raw = '{"scenes":[{"scene_no":1,"objective":"确认“异常账目”与罗盘有关"}]}'

        parsed = parse_llm_json(raw, error_prefix="test")

        self.assertEqual(parsed["scenes"][0]["scene_no"], 1)
        self.assertEqual(parsed["scenes"][0]["objective"], "确认“异常账目”与罗盘有关")

    def test_parse_llm_json_repairs_smart_quotes_used_as_json_syntax(self) -> None:
        raw = '```json\n{“scenes”:[{“scene_no”:1,“objective”:“建立异象”}]}\n```'

        parsed = parse_llm_json(raw, error_prefix="test")

        self.assertEqual(parsed["scenes"][0]["scene_no"], 1)
        self.assertEqual(parsed["scenes"][0]["objective"], "建立异象")

    def test_parse_llm_json_salvages_complete_array_items_from_truncated_object(self) -> None:
        raw = """{
  "state_changes": [
    {
      "entity_name": "沈砚",
      "entity_kind": "character",
      "field": "status",
      "old_value": "未知",
      "new_value": "告密者",
      "reason": "被阿棠发现告密"
    },
    {
      "entity_name": "周岚",
      "entity_kind": "character",
      "field": "location",
      "old_value": "未知",
      "new_value": "旧港"""

        parsed = parse_llm_json(raw, error_prefix="test")

        self.assertEqual(
            parsed,
            {
                "state_changes": [
                    {
                        "entity_name": "沈砚",
                        "entity_kind": "character",
                        "field": "status",
                        "old_value": "未知",
                        "new_value": "告密者",
                        "reason": "被阿棠发现告密",
                    }
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
