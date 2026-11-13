import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("project_runner", ROOT / "scripts/run_project.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ProjectRunnerTest(unittest.TestCase):
    def test_packaged_weights_match_manifest(self):
        self.assertEqual(len(runner.verify_models(ROOT)), 2)

    def test_missing_and_changed_weights_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'models').mkdir()
            (root / 'models/manifest.json').write_text(json.dumps({'files': {'test.pt': hashlib.sha256(b'original').hexdigest()}}))
            with self.assertRaisesRegex(ValueError, 'Missing'):
                runner.verify_models(root)
            (root / 'models/test.pt').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                runner.verify_models(root)

    def test_existing_results_and_input_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input'
            source.mkdir()
            output = root / 'outputs/experiment'
            runner.validate_paths(source, output, root)
            output.mkdir(parents=True)
            (output / 'existing.txt').write_text('keep')
            with self.assertRaisesRegex(ValueError, 'not empty'):
                runner.validate_paths(source, output, root)
            with self.assertRaisesRegex(ValueError, 'inside the input'):
                runner.validate_paths(source, source / 'results', root)
            with self.assertRaisesRegex(ValueError, 'inside the output'):
                runner.validate_paths(output, root / 'outputs', root)


if __name__ == '__main__':
    unittest.main()
