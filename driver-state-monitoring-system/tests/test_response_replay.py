import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ResponseReplayTest(unittest.TestCase):
    def replay(self, events):
        # Synthetic unit-test features, never labels for the recorded participants.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = root / 'records.jsonl'
            records.write_text('\n'.join(json.dumps(dict(timestamp=i / 10, derived=dict(
                face_readable=True, eyes_readable=True, pose_readable=True,
                mouth_open=False, distress_gesture=True))) for i in range(81)))
            event_file = root / 'events.json'
            event_file.write_text(json.dumps(events))
            output = root / 'decisions.jsonl'
            rules = root / 'legacy_rules.json'
            rules.write_text(json.dumps(dict(danger_policy='external_response')))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/replay_response_events.py'),
                                     '--records', str(records), '--events', str(event_file),
                                     '--output', str(output), '--rules', str(rules)], capture_output=True, text=True)
            return result, [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []

    def test_no_event_does_not_infer_timeout(self):
        result, rows = self.replay([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('danger', {r['state'] for r in rows})
        self.assertTrue(all(r['response_received'] is None for r in rows))

    def test_explicit_timeout_and_button_are_distinct(self):
        for kind, expected in [('response_timeout', 'danger'), ('button_response', 'caution')]:
            result, rows = self.replay([dict(timestamp=0, kind='warning_started'),
                                       dict(timestamp=5.1, kind=kind)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(rows[-1]['state'], expected)

    def test_timeout_without_warning_rejected(self):
        result, rows = self.replay([dict(timestamp=0, kind='response_timeout')])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('active warning', result.stderr)
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
