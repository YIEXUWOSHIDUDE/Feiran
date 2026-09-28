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

    def test_a_translation_may_name_in_chinese_what_the_source_says_in_english(self):
        fact = ("Designed and documented REST APIs linking the frontend, backend, document parser, "
                "and object storage to support team integration and deployment.")
        tags = ["REST APIs", "APIs", "API design"]
        vocabulary = tags + ["API", "前端", "后端", "接口", "测试"]
        faithful = check_rewrite("设计并记录 REST API，连接前端、后端、文档解析器和对象存储，以支持团队集成和部署。",
                                 fact, tags, vocabulary)
        singular = check_rewrite("Designed and documented a REST API linking the frontend and backend.", fact, tags, vocabulary)
        added = check_rewrite("设计 REST API，连接前端和后端，并编写测试。", fact, tags, vocabulary)
        self.assertEqual(faithful, [])
        self.assertEqual(singular, [])
        # Testing is not in the source in any language, so it is still a new claim.
        self.assertEqual(added, ["原事实中没有这个技术或技能词：测试"])

    def test_changes_of_meaning_are_rejected_even_without_new_words_of_a_known_kind(self):
        # The first four come from the 2026-09 review: each passed the older checks.
        cases = [
            ("Built 2 services in 3 weeks.", "Built 3 services in 2 weeks."),
            ("Built a prototype used by 2 testers.", "Built a production system used by 2 million customers."),
            ("Studied deployment; did not deploy the services.", "Deployed the services to production."),
            ("Managed a class project.", "Led the engineering organization."),
            ("Studied deployment; did not deploy the services.", "将这些服务部署到生产环境。"),
            # Each of these is caught by one rule alone.
            ("Built a tool used by 2 testers.", "Built a tool used by 2 million testers."),
            ("Did not deploy the services.", "Deployed the services."),
            ("Helped build the billing service.", "Built the billing service."),
            ("Coursework (in progress): Algorithms", "Coursework: Algorithms"),
            ("Managed the release checklist.", "Led the release checklist."),
            # Found in review by Codex (gpt-6-astra, 2026-09-27).
            ("Built 2 services in 3 weeks.", "Built 3 backend services in 2 calendar weeks."),
            ("Built 2 services in 3 weeks.", "2 周内完成 3 个服务。"),
            ("Built the service but did not deploy it.", "Built and deployed the service without downtime."),
            ("不使用第三方库构建解析器。", "使用第三方库构建解析器。"),
            ("为2亿用户提供服务。", "Served 2 billion users."),
            ("没有部署服务。", "部署服务且没有停机。"),  # found in review of PR #1 by Codex
            ("Did not deploy the service.", "Did not test but deployed the service."),  # found in review of PR #2
            ("没有部署服务。", "没有测试但部署服务。"),
            ("Have not yet deployed the service.", "Deployed the service without downtime."),  # review of the fixes
            ("没有同时部署服务。", "部署服务且没有停机。"),
        ]
        for source, rewrite in cases:
            with self.subTest(rewrite=rewrite):
                self.assertNotEqual(check_rewrite(rewrite, source), [])

    def test_rewordings_and_translations_that_keep_the_meaning_still_pass(self):
        cases = [
            ("Built 2 services in 3 weeks.", "In 3 weeks, built 2 services."),
            ("Built 2 services in 3 weeks.", "3 周内完成 2 个服务。"),
            ("Built a prototype used by 2 testers.", "Built a prototype that 2 testers used."),
            ("Built a prototype used by 2 testers.", "开发了供 2 名测试人员使用的原型。"),
            ("Studied deployment; did not deploy the services.", "Studied deployment but did not deploy the services."),
            ("Studied deployment; did not deploy the services.", "学习了部署，但没有部署这些服务。"),
            ("Built the parser without third-party libraries.", "不依赖第三方库构建解析器。"),
            ("Managed a class project.", "Managed a course project."),
            ("Managed a class project.", "管理一个课程项目。"),
            ("Led REST API design with 2 teammates.", "带领 2 名队友设计 REST API。"),
            ("Trained machine learning models for classification.", "训练用于分类的机器学习模型。"),
            ("训练用于分类的机器学习模型。", "Trained machine learning models for classification."),
            ("Not only built but also tested the parser.", "Built and tested the parser."),
            ("Built tools for customers.", "为客户构建工具。"),
            ("Built tools for clients.", "为客户构建工具。"),
            ("Built 3 backend services in 2 weeks.", "In 2 weeks, built 3 services."),
            ("Did not use third-party libraries.", "Built without using third-party libraries."),
            ("不断优化查询性能。", "Continuously optimized query performance."),
            ("Did not use third-party libraries.", "No third-party libraries were used."),  # found by Codex
            ("Did not use third-party libraries.", "Third-party libraries were not used."),
            ("没有部署服务。", "没有部署这些服务。"),
            ("没有部署服务。", "Did not deploy the services."),
            ("Have not yet deployed the service.", "Did not yet deploy the service."),
        ]
        for source, rewrite in cases:
            with self.subTest(rewrite=rewrite):
                self.assertEqual(check_rewrite(rewrite, source), [])

    def test_malformed_lines_are_rejected(self):
        for text in ["  ", "Designed REST APIs.\nAnd more.", "Designed REST APIs, see https://example.com.", "x" * 301]:
            with self.subTest(text=text[:24]):
                self.assertNotEqual(check_rewrite(text, API_FACT), [])


if __name__ == "__main__":
    unittest.main()
