import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "codex-exec-stream.py"
SPEC = importlib.util.spec_from_file_location("codex_exec_stream", MODULE_PATH)
stream = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stream)


class CodexExecStreamTest(unittest.TestCase):
    def test_reports_only_existing_loaded_skill_and_preserves_codex_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "awesome-api-design"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: awesome-api-design\n---\n")
            code = (
                "import sys; "
                "print('{\"type\":\"turn.started\"}', flush=True); "
                "print('CODEX_MVP_SKILL_LOADED\\tawesome-api-design', file=sys.stderr, flush=True); "
                "print('CODEX_MVP_SKILL_LOADED\\tmissing-skill', file=sys.stderr, flush=True); "
                "print('{\"type\":\"turn.completed\"}', flush=True)"
            )
            exit_code = stream.run([sys.executable, "-c", code], root / "codex-events.jsonl",
                                   root / "codex-stderr.log", root / "skills")
            self.assertEqual(exit_code, 0)
            events = [json.loads(line) for line in (root / "codex-events.jsonl").read_text().splitlines()]
            self.assertEqual([event["type"] for event in events].count("skill.loaded"), 1)
            self.assertIn("turn.started", {event["type"] for event in events})
            self.assertIn("turn.completed", {event["type"] for event in events})
            self.assertEqual(json.loads((root / "loaded-skills.json").read_text()), ["awesome-api-design"])


if __name__ == "__main__":
    unittest.main()
