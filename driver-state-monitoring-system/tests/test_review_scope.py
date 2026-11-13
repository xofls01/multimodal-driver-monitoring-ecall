import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReviewScopeTest(unittest.TestCase):
    def test_twenty_scenarios_without_condition_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inspection = root / 'inspection'
            inspection.mkdir()
            rows = [dict(clip_id=f'{family}{i}', scenario_id=f'{family}{i}', scenario_family=family,
                         capture_condition='N', relative_path=f'{family}/{family}{i}.mp4',
                         width=1280, height=720, fps=30, contact_sheet=f'{family}{i}.jpg')
                    for family in 'NDAE' for i in range(1,6)]
            with (inspection / 'video_metadata.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader(); writer.writerows(rows)
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/build_vision_review.py'),
                                     '--output', str(root), '--scenario-only'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            page = (root / 'index.html').read_text()
            self.assertEqual(page.count('<article>'), 20)
            self.assertEqual(page.count('<section>'), 4)
            self.assertNotIn('<details', page)
            self.assertNotIn('한 손 운전', page)
            with (root / 'condition_coverage.csv').open(encoding='utf-8-sig') as stream:
                coverage = list(csv.DictReader(stream))
            self.assertEqual(len(coverage), 4)
            self.assertTrue(all(r['found_videos']=='5' and r['capture_condition']=='not_stratified' for r in coverage))


if __name__ == '__main__':
    unittest.main()
