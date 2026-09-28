import unittest

from privacy import mask, private_terms


class PrivacyTests(unittest.TestCase):
    def test_every_language_of_the_name_and_schools_is_private(self):
        profile = {"name": {"en": "Alex Example", "zh": "示例"}, "contact": {},
                   "sections": [{"kind": "education", "entries": [{"title": {"en": "Example University", "zh": "示例大学"}}]}]}
        masked = mask("Alex Example built APIs at Example University; 示例在示例大学。", private_terms(profile))
        self.assertNotIn("Example", masked)
        self.assertNotIn("示例", masked)

    def test_a_link_label_like_github_is_not_private_but_the_address_is(self):
        # Found in review by Codex: a contact link labeled GitHub hid "GitHub Actions" everywhere.
        profile = {"name": "Alex Example", "sections": [], "contact": {"links": [
            {"label": "GitHub", "url": "https://github.com/alex-example"},
            {"label": "github.com/alex-example", "url": "https://github.com/alex-example"}]}}
        masked = mask("Built CI with GitHub Actions; code at github.com/alex-example", private_terms(profile))
        self.assertEqual(masked, "Built CI with GitHub Actions; code at [link]")


if __name__ == "__main__":
    unittest.main()
