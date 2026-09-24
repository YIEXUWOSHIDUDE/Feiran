import unittest

from claims import check_rewrite


API_FACT = (
    "Designed and documented REST APIs linking the frontend, backend, document parser, "
    "and object storage to support team integration and deployment."
)


class ClaimCheckTests(unittest.TestCase):
    def test_faithful_rewrite_passes(self):
        rewrite = (
            "Designed and documented REST APIs connecting the frontend, backend, document parser, "
            "and object storage for team integration and deployment."
        )
        self.assertEqual(check_rewrite(rewrite, API_FACT), [])

    def test_new_numbers_are_rejected(self):
        reasons = check_rewrite("Designed 12 REST APIs used by 3 teams.", API_FACT)
        self.assertTrue(any("12" in reason for reason in reasons))
        self.assertTrue(any("3" in reason for reason in reasons))

    def test_technologies_missing_from_the_fact_are_rejected(self):
        reasons = check_rewrite(
            "Designed REST APIs in FastAPI and deployed them with Docker.",
            API_FACT,
            vocabulary=["Docker", "Python"],
        )
        self.assertTrue(any("FastAPI" in reason for reason in reasons))
        self.assertTrue(any("Docker" in reason for reason in reasons))
        self.assertFalse(any("REST" in reason for reason in reasons))

    def test_leadership_words_need_support_in_the_fact(self):
        english = check_rewrite("Led the design of REST APIs for team integration.", API_FACT)
        chinese = check_rewrite("主导设计并编写 REST APIs 文档，连接前后端与对象存储。", API_FACT)
        supported = check_rewrite(
            "Led REST API design for team integration.",
            "Led REST API design for team integration and deployment.",
        )
        self.assertTrue(any("Led" in reason for reason in english))
        self.assertTrue(any("主导" in reason for reason in chinese))
        self.assertEqual(supported, [])

    def test_chinese_translation_may_use_only_the_facts_own_tags(self):
        fact = (
            "Refactored legacy Java business systems, investigated defects, "
            "and performed functional testing to validate changes."
        )
        tags = ["Java", "refactoring", "testing", "测试", "重构"]
        vocabulary = tags + ["后端", "Docker"]
        faithful = check_rewrite("重构遗留 Java 业务系统，排查缺陷并进行功能测试以验证变更。", fact, tags, vocabulary)
        added = check_rewrite("重构遗留 Java 后端业务系统并进行功能测试。", fact, tags, vocabulary)
        self.assertEqual(faithful, [])
        self.assertTrue(any("后端" in reason for reason in added))

    def test_malformed_lines_are_rejected(self):
        for text in ["  ", "Designed REST APIs.\nAnd more.", "Designed REST APIs, see https://example.com.", "x" * 301]:
            with self.subTest(text=text[:24]):
                self.assertNotEqual(check_rewrite(text, API_FACT), [])


if __name__ == "__main__":
    unittest.main()
