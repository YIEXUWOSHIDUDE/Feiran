import re
import unittest
from urllib.parse import unquote_to_bytes

from feiran.download_name import pdf_disposition
from tests import test_web, test_web_v2


class DownloadNameTests(unittest.TestCase):
    def test_names_include_job_identity_language_and_content_version(self):
        disposition = pdf_disposition(company="Acme", title="Backend Intern", language="en",
                                      job_id="a1b2c3d4e5", version="0123456789abcdef")

        self.assertIn('filename="CV-Acme-Backend-Intern-EN-a1b2c3d4-v01234567.pdf"', disposition)
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition).group(1)
        self.assertEqual(unquote_to_bytes(encoded).decode(), "CV-Acme-Backend Intern-EN-a1b2c3d4-v01234567.pdf")

    def test_unicode_is_preserved_in_extended_name_and_ascii_fallback_is_safe(self):
        disposition = pdf_disposition(company="字节/科技", title="后端工程师", language="zh",
                                      job_id="12345678", version="abcdef123456")

        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition).group(1)
        self.assertEqual(unquote_to_bytes(encoded).decode(), "CV-字节 科技-后端工程师-ZH-12345678-vabcdef12.pdf")
        fallback = re.search(r'filename="([^"]+)"', disposition).group(1)
        self.assertTrue(fallback.isascii())
        self.assertNotIn("/", fallback)
        self.assertNotIn("\\", fallback)

    def test_untrusted_filename_parts_cannot_inject_headers_or_paths(self):
        disposition = pdf_disposition(company='Acme\r\nX-Evil: yes\\..\\', title='../Role\nName',
                                      language="en", job_id="../../../../job-123", version="hash\r\n")

        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertNotIn("\\", disposition)
        self.assertNotIn("../", disposition)
        self.assertEqual(disposition.count("attachment;"), 1)

    def test_missing_fields_have_stable_defaults(self):
        disposition = pdf_disposition(company=None, title="", language="xx", job_id="", version=None)

        self.assertIn('filename="CV-Job-Job-XX-unknown-vunknown.pdf"', disposition)
        self.assertIn("filename*=UTF-8''CV-Job-Job-XX-unknown-vunknown.pdf", disposition)

    def test_long_inputs_keep_the_header_bounded_and_keep_pdf_suffix(self):
        disposition = pdf_disposition(company="公司" * 1000, title="工程师" * 1000, language="en",
                                      job_id="a" * 1000, version="b" * 1000)

        self.assertLessEqual(len(disposition), 700)
        self.assertTrue(disposition.endswith(".pdf"))


def names(response):
    """(ASCII fallback, UTF-8 name) of a download response."""
    header = response.headers["content-disposition"]
    fallback = re.search(r'filename="([^"]+)"', header).group(1)
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", header).group(1)
    return fallback, unquote_to_bytes(encoded).decode()


class V2DownloadTests(test_web_v2.V2Case):
    def printed(self, client, headers, job_id):
        self.assertEqual(self.approve(client, headers, job_id).status_code, 200)
        self.assertEqual(self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))
                         ["status"], "succeeded")
        response = client.get(f"/download/{job_id}/en.pdf", headers=headers)
        self.assertEqual((response.status_code, response.content[:4]), (200, b"%PDF"))
        return response

    def test_each_job_and_each_version_downloads_under_its_own_name(self):
        client, headers = self.ready_user()
        first = self.new_job(client, headers)
        created = client.post("/api/jobs", headers=headers, json={
            "title": "Data Intern", "company": "Other Co", "text": test_web_v2.JD_TEXT + "\n- 熟悉数据分析"})
        second = self.settle(client, headers, created)["result"]["job_id"]
        one, two = self.printed(client, headers, first), self.printed(client, headers, second)
        fallback, name = names(one)
        cv = client.get(f"/api/jobs/{first}", headers=headers).json()["cv"]["en"]
        self.assertEqual(name, f"CV-示例公司-后端开发实习生-EN-{first[:8]}-v{cv['content_sha256'][:8]}.pdf")
        self.assertTrue(fallback.isascii() and fallback.endswith(f"-EN-{first[:8]}-v{cv['content_sha256'][:8]}.pdf"))
        self.assertNotEqual(names(one), names(two))
        line = cv["lines"][0]
        client.post(f"/api/jobs/{first}/cv/en/lines/{line['block']}", headers=headers,
                    json={"text": line["text"] + " Updated.", "expected_text": line["text"]})
        self.assertEqual(client.get(f"/download/{first}/en.pdf", headers=headers).status_code, 404)  # not approved
        again = self.printed(client, headers, first)
        self.assertNotEqual(names(again), names(one))  # a new version, a new name
        self.assertEqual(names(again)[1].split("-EN-")[0], names(one)[1].split("-EN-")[0])


class V1DownloadTests(test_web.WebTests):
    def test_the_single_user_app_names_downloads_the_same_way(self):
        job_id = self.planned_job()
        cv = self.job(job_id)["cv"]["en"]
        self.assertEqual(self.approve(job_id, cv["content_sha256"]).status_code, 200)
        self.client.post(f"/api/jobs/{job_id}/cv/en/export", headers=self.headers)
        response = self.client.get(f"/download/{job_id}/en.pdf", params={"token": test_web.TOKEN})
        self.assertEqual(response.status_code, 200, response.text)
        fallback, name = names(response)
        self.assertEqual(name, f"CV-Job-Test Engineer-EN-{job_id[:8]}-v{cv['content_sha256'][:8]}.pdf")
        self.assertEqual(fallback, f"CV-Job-Test-Engineer-EN-{job_id[:8]}-v{cv['content_sha256'][:8]}.pdf")


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (DownloadNameTests, V2DownloadTests, V1DownloadTests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
