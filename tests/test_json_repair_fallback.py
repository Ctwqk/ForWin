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


if __name__ == "__main__":
    unittest.main()
