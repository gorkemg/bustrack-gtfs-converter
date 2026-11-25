"""Static guards for the publishing pipeline (overview/git-branching-model.md).

`main` is integration only: nothing may publish from it. Publishing code comes from the `production` or `staging`
pointers, which promote.yml fast-forwards (production only by the owner).
"""

import pathlib
import re
import unittest

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
PUBLISHING = ("gtfs-sync-release.yml", "gtfs-staging-prerelease.yml", "motis-data.yml", "rollback.yml")


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def push_branches(text: str) -> list[str]:
    match = re.search(r"^  push:\n    branches:\n((?:      - .+\n)+)", text, re.M)
    return [line.strip()[2:].strip() for line in match.group(1).splitlines()] if match else []


class PublishingPipelineTest(unittest.TestCase):
    def test_no_publishing_workflow_listens_to_main_pushes(self):
        for name in PUBLISHING:
            self.assertNotIn("main", push_branches(read(name)), name)
        self.assertEqual(push_branches(read("gtfs-sync-release.yml")), ["production"])
        self.assertEqual(push_branches(read("gtfs-staging-prerelease.yml")), ["staging"])
        self.assertFalse((WORKFLOWS / "gtfs-beta-prerelease.yml").exists())

    def test_publishing_checkouts_use_a_pointer(self):
        for name in PUBLISHING:
            text = read(name)
            for block in re.findall(r"uses: actions/checkout@[^\n]+\n((?:        .+\n)+)", text):
                self.assertRegex(block, r"ref: .*(production|SOURCE_REF)", name)

    def test_motis_follows_production_runs(self):
        self.assertIn("branches: [production, main]", read("motis-data.yml"))

    def test_promote_is_fast_forward_only_and_owner_gated(self):
        text = read("promote.yml")
        self.assertIn("environment: ${{ inputs.target }}", text)
        self.assertIn('"$TRIGGERING_ACTOR" != "gorkemg"', text)
        self.assertIn("merge-base --is-ancestor", text)
        self.assertNotIn("--force", text)
        self.assertIn("ssh-key: ${{ secrets.PROMOTE_SSH_KEY }}", text)

    def test_pointer_rollback_is_owner_gated_and_tag_bound(self):
        text = read("rollback-pointer.yml")
        self.assertIn('"$TRIGGERING_ACTOR" != "gorkemg"', text)
        self.assertIn("^prod-[0-9]{8}-[0-9]{4}", text)


if __name__ == "__main__":
    unittest.main()
