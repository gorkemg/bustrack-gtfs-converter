"""Guard the branch restrictions that protect release mutations."""
from pathlib import Path
import unittest


class WorkflowSafetyTests(unittest.TestCase):
    def test_publication_only_uses_main(self):
        workflow = Path('.github/workflows/gtfs-sync-release.yml').read_text()
        self.assertIn('  push:\n    branches:\n      - main\n    paths:', workflow)
        self.assertIn("  sync:\n    if: github.ref == 'refs/heads/main'\n", workflow)

    def test_rollback_checks_ref_before_mutating(self):
        workflow = Path('.github/workflows/rollback.yml').read_text()
        guard = workflow.index('if [ "$GITHUB_REF" != "refs/heads/main" ]; then')
        failure = workflow.index('exit 1', guard)
        self.assertLess(failure, workflow.index('gh release upload'))


if __name__ == '__main__':
    unittest.main()
