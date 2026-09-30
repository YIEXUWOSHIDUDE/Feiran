"""Synthetic HTML and network responses only; never contacts a real website."""
import json
import gzip
import socket
import unittest
from email.message import Message
from unittest.mock import Mock, patch

from job_search import SearchError, parse_posting_link
from job_url import (_connect, _public_ip, extract_job, fetch_html, fetch_job_url,
                     normalize_url, MAX_PAGE_BYTES)

URL = "https://careers.example.org/jobs/backend?id=42"
DESCRIPTION = "Build Python backend services and maintain reliable APIs for our product team. Collaborate with engineers to test and improve database performance."
HTML = f"<html><head><title>Backend Engineer</title></head><body><nav>Navigation</nav><main><h1>Backend Engineer</h1><h2>Responsibilities</h2><p>{DESCRIPTION}</p><h2>Requirements</h2><p>Python and SQL experience.</p></main><footer>Footer</footer></body></html>"


def schema_page(**overrides):
    job = {"@type": "JobPosting", "title": "后端开发工程师", "description": f"<p>{DESCRIPTION}</p>",
           "hiringOrganization": {"name": "Example"}, "datePosted": "2026-09-30",
           "jobLocation": {"address": {"addressLocality": "北京"}}, **overrides}
    return '<script type="application/ld+json">' + json.dumps({"@graph": [job]}) + '</script>'


def response(body=HTML, status=200, location=None, content_type="text/html; charset=utf-8"):
    headers = Message()
    headers["Content-Type"] = content_type
    if location:
        headers["Location"] = location
    raw = body.encode("utf-8") if isinstance(body, str) else body
    result = Mock(status=status, headers=headers)
    result.getheader.side_effect = lambda name, default=None: headers.get(name, default)
    result.read1.side_effect = [raw, b""]
    return result


class ExtractionTests(unittest.TestCase):
    def test_plain_page_uses_main_and_preserves_unknowns(self):
        jd = extract_job(HTML, URL)["jd"]
        self.assertEqual(jd["title"], "Backend Engineer")
        self.assertEqual(jd["extraction_method"], "html-text")
        self.assertIn(DESCRIPTION, jd["text"])
        self.assertNotIn("Navigation", jd["text"])
        self.assertNotIn("Footer", jd["text"])
        self.assertIsNone(jd["company"])
        self.assertIsNone(jd["posted_at"])

    def test_jsonld_graph_reads_job_fields_and_extra_requirements(self):
        jd = extract_job(schema_page(qualifications="Experience with SQL"), URL)["jd"]
        self.assertEqual((jd["title"], jd["company"], jd["location"]), ("后端开发工程师", "Example", "北京"))
        self.assertEqual(jd["posted_at"], "2026-09-30")
        self.assertIn("Qualifications\nExperience with SQL", jd["text"])
        self.assertEqual(jd["extraction_method"], "jobposting-jsonld")

    def test_hidden_navigation_scripts_and_forms_are_not_jd(self):
        html = HTML.replace("</main>", "<div hidden>Secret</div><script>alert('Bad')</script><form>Apply now</form></main>")
        text = extract_job(html, URL)["jd"]["text"]
        for excluded in ("Secret", "Bad", "Apply now"):
            self.assertNotIn(excluded, text)

    def test_multiple_jobs_shell_login_and_truncated_content_fail(self):
        for html in (schema_page() + schema_page(title="Another job"),
                     '<title>Jobs</title><div id="app"></div><script>loadJobs()</script>',
                     '<title>Sign in</title><form>Username Password</form>',
                     schema_page(description="Tiny")):
            with self.subTest(html=html[:30]), self.assertRaises(SearchError):
                extract_job(html, URL)

    def test_malformed_jsonld_falls_back_to_visible_text(self):
        self.assertEqual(extract_job('<script type="application/ld+json">invalid</script>' + HTML, URL)["jd"]["title"], "Backend Engineer")

    def test_unrelated_banner_heading_does_not_pollute_document_title(self):
        html = HTML.replace("<body>", "<body><h1>29 jobs on our job board</h1>")
        self.assertEqual(extract_job(html, URL)["jd"]["title"], "Backend Engineer")

    def test_chinese_plain_page_and_inline_markup(self):
        html = '<h1>后端开发</h1><div><h2>岗位职责</h2><p>' + '负责开发和维护后端服务，与团队一起完成需求分析和接口测试。' * 4 + '</p><h2>岗位要求</h2>熟悉 <b>Python</b> 和数据库。</div>'
        self.assertIn("熟悉 Python 和数据库。", extract_job(html, URL)["jd"]["text"])

    @patch("job_url.fetch_html", return_value=(HTML, URL))
    def test_final_source_and_original_url_both_retained(self, read):
        result = fetch_job_url("https://example.org/redirect#fragment")
        self.assertEqual(result["jd"]["source"], URL)
        self.assertEqual(result["jd"]["requested_url"], "https://example.org/redirect")

    def test_tencent_link_uses_existing_official_adapter(self):
        self.assertEqual(parse_posting_link("https://careers.tencent.com/zh-cn/jobdesc.html?postId=2006202335759585280"),
                         ("tencent", "tencent", "2006202335759585280"))


class PublicURLTests(unittest.TestCase):
    def test_arbitrary_public_host_and_unicode_path_are_allowed(self):
        self.assertEqual(normalize_url("careers.example.org/jobs/后端#apply"),
                         "https://careers.example.org/jobs/%E5%90%8E%E7%AB%AF")

    def test_local_private_credentials_schemes_ports_and_controls_rejected(self):
        for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://localhost/", "http://foo.local/",
                    "file:///etc/passwd", "ftp://example.org/job", "https://user:pass@example.org/job",
                    "https://example.org:8000/job", "https://example.org/\r\nInjected", "https://[broken/",
                    "http://224.0.0.1/", "https://example.org\\@localhost/job"):
            with self.subTest(url=url), self.assertRaises(SearchError):
                normalize_url(url)

    @patch("job_url.socket.getaddrinfo")
    def test_dns_private_or_mixed_answers_rejected(self, resolve):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        for records in ([private], [public, private]):
            resolve.return_value = records
            with self.assertRaises(SearchError):
                _public_ip("careers.example.org", 443)
        resolve.return_value = [public]
        self.assertEqual(_public_ip("careers.example.org", 443), "93.184.216.34")

    @patch("job_url.ssl.create_default_context")
    @patch("job_url.socket.create_connection")
    def test_connection_pins_ip_and_checks_tls_original_hostname(self, connect, context):
        connection = _connect("careers.example.org", 443, "93.184.216.34", True)
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        context.return_value.wrap_socket.assert_called_once_with(connect.return_value, server_hostname="careers.example.org")
        connection.close()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.ip = patch("job_url._public_ip", return_value="93.184.216.34").start()
        self.connect = patch("job_url._connect").start()
        self.addCleanup(patch.stopall)

    def test_html_and_no_credentials_headers(self):
        self.connect.return_value.getresponse.return_value = response()
        html, source = fetch_html(URL)
        self.assertEqual((html, source), (HTML, URL))
        headers = self.connect.return_value.request.call_args.kwargs["headers"]
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)
        self.connect.return_value.close.assert_called_once()

    def test_redirect_rechecks_target_and_blocks_private(self):
        self.connect.return_value.getresponse.return_value = response(status=302, location="http://127.0.0.1/secret")
        with self.assertRaisesRegex(SearchError, "公网"):
            fetch_html(URL)
        self.assertEqual(self.connect.call_count, 1)

    def test_relative_redirect_and_dns_are_checked_each_hop(self):
        self.connect.return_value.getresponse.side_effect = [response(status=302, location="/new"), response()]
        self.assertEqual(fetch_html(URL)[1], "https://careers.example.org/new")
        self.assertEqual(self.ip.call_count, 2)

    def test_status_type_limit_encoding_and_timeout_fail_explicitly(self):
        samples = [response(status=403), response(status=404), response(content_type="application/pdf"),
                   response(body=b"x" * (MAX_PAGE_BYTES + 1)), response(body=b"\xff", content_type="text/html; charset=utf-8")]
        for sample in samples:
            self.connect.return_value.getresponse.return_value = sample
            with self.subTest(status=sample.status), self.assertRaises(SearchError):
                fetch_html(URL)
        self.connect.return_value.getresponse.side_effect = TimeoutError()
        with self.assertRaisesRegex(SearchError, "超时"):
            fetch_html(URL)

    def test_redirect_loop_is_bounded(self):
        self.connect.return_value.getresponse.return_value = response(status=302, location="/loop")
        with self.assertRaisesRegex(SearchError, "重定向"):
            fetch_html(URL)
        self.assertEqual(self.connect.call_count, 5)

    def test_gzip_decodes_but_expansion_is_bounded(self):
        for body, valid in ((HTML.encode(), True), (b"x" * (MAX_PAGE_BYTES + 1), False)):
            compressed = response(body=gzip.compress(body))
            compressed.headers["Content-Encoding"] = "gzip"
            self.connect.return_value.getresponse.return_value = compressed
            if valid:
                self.assertEqual(fetch_html(URL)[0], HTML)
            else:
                with self.assertRaisesRegex(SearchError, "过大"):
                    fetch_html(URL)


if __name__ == "__main__":
    unittest.main()
