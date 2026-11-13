"""Regression coverage for repeated launches without changing the model."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from run_driver_model import archive_transport_log, lock_output, prepare_launch


class ModelLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.model = self.root / "driver-state-monitoring-system"
        script = self.model / "scripts/run_multimodal_system.py"
        script.parent.mkdir(parents=True)
        script.write_text("# Original model source\n")
        python = self.model / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("test interpreter placeholder")
        pairs = []
        for name in ("E4", "D3"):
            folder = self.model / "data" / name
            folder.mkdir(parents=True)
            (folder / "video.mp4").write_bytes(b"recording " + name.encode())
            for filename in ("biosignals.csv", "frames.csv", "session.json", "vitals.csv"):
                (folder / filename).write_text("input " + filename)
            pairs.append(dict(id=name, video=f"data/{name}/video.mp4", biosignal_folder=f"data/{name}",
                              bio_offset_seconds=0.0, sync_basis="synthetic_clip_clock"))
        self.manifest = self.model / "config/multimodal/system_pairs.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps(dict(pairs=pairs)))

    def launch(self, *arguments):
        return prepare_launch(list(arguments), project_root=self.root)

    def test_repeated_launch_reuses_dataset_folder_and_preserves_transport_history(self):
        original = self.model / "outputs/multimodal_output_contract/unity_transport.jsonl"
        original.parent.mkdir(parents=True)
        original.write_text("existing analysis\n")
        first, _, _ = self.launch("--only", "E4", "--unity", "--show")
        first_output = Path(first[-1])
        with (first_output / "unity_transport.jsonl").open("x") as log:
            log.write("first new run\n")
        second, _, _ = self.launch("--only", "E4", "--unity", "--show")
        second_output = Path(second[-1])
        self.assertEqual(first_output, second_output)
        self.assertTrue(archive_transport_log(second_output))
        with (second_output / "unity_transport.jsonl").open("x") as log:
            log.write("second new run\n")
        self.assertEqual(original.read_text(), "existing analysis\n")
        self.assertEqual((first_output / "unity_transport.history.jsonl").read_text(), "first new run\n")
        self.assertTrue(archive_transport_log(second_output))
        self.assertEqual((first_output / "unity_transport.history.jsonl").read_text(),
                         "first new run\nsecond new run\n")
        self.assertFalse(archive_transport_log(second_output))
        self.assertEqual((self.model / "scripts/run_multimodal_system.py").read_text(), "# Original model source\n")

    def test_new_video_contents_create_new_folder_without_changing_old_results(self):
        first, _, _ = self.launch("--only", "E4")
        old_result = Path(first[-1]) / "saved-result.txt"
        old_result.write_text("previous result")
        (self.model / "data/E4/video.mp4").write_bytes(b"a different recording")
        second, _, _ = self.launch("--only", "E4")
        self.assertNotEqual(first[-1], second[-1])
        self.assertEqual(old_result.read_text(), "previous result")

    def test_signal_and_timing_changes_create_new_folder(self):
        for name in ("biosignals.csv", "frames.csv", "session.json", "vitals.csv"):
            with self.subTest(name=name):
                path = self.model / "data/E4" / name
                original = path.read_bytes()
                before, _, _ = self.launch("--only", "E4")
                path.write_bytes(original + b"\nnew sample")
                after, _, _ = self.launch("--only", "E4")
                self.assertNotEqual(before[-1], after[-1])
                path.write_bytes(original)

    def test_display_transport_flags_and_selection_order_do_not_create_new_data(self):
        first, _, _ = self.launch("--only", "E4", "D3", "--show")
        second, _, _ = self.launch("--only", "D3", "E4", "--unity", "--unity-port", "5100")
        self.assertEqual(first[-1], second[-1])

    def test_same_dataset_cannot_run_twice_concurrently(self):
        command, _, _ = self.launch("--only", "E4")
        output = Path(command[-1])
        descriptor = lock_output(output)
        try:
            self.assertTrue(os.get_inheritable(descriptor))
            with self.assertRaisesRegex(ValueError, "이미 실행 중"):
                lock_output(output)
        finally:
            os.close(descriptor)
        descriptor = lock_output(output)
        os.close(descriptor)

    def test_model_options_are_forwarded_and_unity_defaults_to_separate_port(self):
        command, environment, working_directory = self.launch("--only", "D3", "--unity", "--show")
        self.assertIn("--show", command)
        self.assertEqual(command[command.index("--only") + 1], "D3")
        self.assertEqual(command[command.index("--unity-port") + 1], "5002")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(working_directory, self.model)

    def test_explicit_model_port_is_preserved(self):
        for arguments in (("--unity-port", "5100"), ("--unity-port=5100",)):
            with self.subTest(arguments=arguments):
                command, _, _ = self.launch("--unity", *arguments)
                self.assertNotIn("5002", command)
                self.assertTrue(all(argument in command for argument in arguments))

    def test_standalone_visualization_does_not_enable_unity(self):
        command, _, _ = self.launch("--only", "E4", "--show")
        self.assertNotIn("--unity", command)
        self.assertNotIn("--unity-port", command)

    def test_explicit_nonempty_result_directory_is_rejected_without_data_loss(self):
        folder = self.root / "existing"
        folder.mkdir()
        saved = folder / "unity_transport.jsonl"
        saved.write_text("saved results")
        with self.assertRaisesRegex(ValueError, "기존 결과를 보존"):
            self.launch("--output", str(folder), "--unity")
        self.assertEqual(saved.read_text(), "saved results")

    def test_explicit_new_directory_and_custom_interpreter_are_supported(self):
        folder = self.root / "chosen-output"
        custom_python = self.root / "custom-python"
        custom_python.write_text("custom interpreter placeholder")
        command, _, _ = self.launch("--output", str(folder), "--model-python", str(custom_python))
        self.assertEqual(command[0], str(custom_python))
        self.assertEqual(command[-2:], ["--output", str(folder)])
        self.assertTrue(folder.is_dir())

    def test_missing_environment_fails_before_creating_results(self):
        (self.model / ".venv/bin/python").unlink()
        with self.assertRaisesRegex(ValueError, "Python 환경이 없습니다"):
            self.launch("--show")
        self.assertFalse((self.model / "outputs").exists())


if __name__ == "__main__":
    unittest.main()
