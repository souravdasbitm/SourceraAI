import json
import unittest

from core.llm_json import loads_llm_json


class LlmJsonTest(unittest.TestCase):
    def test_repairs_invalid_css_escape(self):
        raw = r'{"steps":[{"action":"click","selector":"div\s span"}]}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        parsed = loads_llm_json(raw)
        self.assertEqual(parsed["steps"][0]["selector"], r"div\s span")

    def test_keeps_real_json_escapes(self):
        parsed = loads_llm_json('{"text":"line\\nnext","path":"C:\\\\temp"}')
        self.assertEqual(parsed["text"], "line\nnext")
        self.assertEqual(parsed["path"], "C:\\temp")


if __name__ == "__main__":
    unittest.main()