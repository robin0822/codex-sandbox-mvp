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

    def test_reports_skill_only_when_completed_command_returns_its_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "awesome-api-design"
            skill.mkdir(parents=True)
            document = "---\nname: awesome-api-design\n---\n# API Design\nRead every rule.\n"
            path = skill / "SKILL.md"
            path.write_text(document)
            completed = {"type": "item.completed", "item": {
                "type": "command_execution", "command": f"cat {path}",
                "aggregated_output": document, "exit_code": 0}}
            failed = {"type": "item.completed", "item": {
                "type": "command_execution", "command": f"cat {path}",
                "aggregated_output": document, "exit_code": 1}}
            incomplete = {"type": "item.completed", "item": {
                "type": "command_execution", "command": f"cat {path}",
                "aggregated_output": document[:30], "exit_code": 0}}
            code = "\n".join(["print(" + repr(json.dumps(event)) + ", flush=True)"
                              for event in [failed, incomplete, completed]])
            exit_code = stream.run([sys.executable, "-c", code], root / "codex-events.jsonl",
                                   root / "codex-stderr.log", root / "skills")
            self.assertEqual(exit_code, 0)
            events = [json.loads(line) for line in (root / "codex-events.jsonl").read_text().splitlines()]
            loaded = [event for event in events if event["type"] == "skill.loaded"]
            self.assertEqual(loaded, [{"type": "skill.loaded", "skill_id": "awesome-api-design",
                                       "source": "command_output"}])
            self.assertEqual(json.loads((root / "loaded-skills.json").read_text()), ["awesome-api-design"])

    def test_reports_explicit_skill_only_when_full_document_is_in_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "awesome-api-design"
            skill.mkdir(parents=True)
            document = "---\nname: awesome-api-design\n---\n# API Design\nRead every rule.\n"
            (skill / "SKILL.md").write_text(document)
            explicit = root / "explicit-skills.json"
            explicit.write_text('["awesome-api-design"]')
            prompt = root / "prompt.txt"
            prompt.write_text("Use this skill:\n" + document + "\nQuestion")
            code = "print('{\"type\":\"thread.started\"}', flush=True)"
            exit_code = stream.run([sys.executable, "-c", code], root / "codex-events.jsonl",
                                   root / "codex-stderr.log", root / "skills", explicit, prompt)
            self.assertEqual(exit_code, 0)
            events = [json.loads(line) for line in (root / "codex-events.jsonl").read_text().splitlines()]
            self.assertEqual(events[0], {"type": "skill.loaded", "skill_id": "awesome-api-design",
                                         "source": "explicit_prompt"})
            self.assertEqual(json.loads((root / "loaded-skills.json").read_text()), ["awesome-api-design"])

            prompt.write_text("Use this skill:\n" + document[:24])
            stream.run([sys.executable, "-c", code], root / "codex-events.jsonl",
                       root / "codex-stderr.log", root / "skills", explicit, prompt)
            events = [json.loads(line) for line in (root / "codex-events.jsonl").read_text().splitlines()]
            self.assertFalse(any(event["type"] == "skill.loaded" for event in events))


if __name__ == "__main__":
    unittest.main()
