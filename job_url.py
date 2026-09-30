"""Read a public job page into the existing JD snapshot, without executing page code.

The web layer owns persistence and generation. This module owns public-URL access
and conservative HTML/JobPosting extraction; it never reads candidate facts.
"""

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import zlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from job_search import SearchError

MAX_PAGE_BYTES = 3_000_000
TIMEOUT = 12
MAX_REDIRECTS = 4
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "nav", "footer", "aside", "form", "noscript", "svg", "template"}
BLOCK = {"p", "div", "li", "br", "section", "h1", "h2", "h3", "h4", "article", "main"}
JOB_SIGNAL = re.compile(r"job description|requirements|qualifications|responsibilities|what you.ll (?:do|bring)|about (?:the|this) (?:role|job)|岗位职责|岗位要求|任职要求|职位描述|工作职责|任职资格|职位要求|任职条件|职责描述|岗位描述", re.I)


def normalize_url(value: str) -> str:
    """Validate HTTP(S) syntax; DNS is checked separately at every connection."""
    value = value.strip()
    if not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise SearchError("请输入有效的公开岗位 URL。")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise SearchError("岗位 URL 格式无效。") from exc
    if (parts.scheme not in ("http", "https") or not host or parts.username is not None or
            parts.password is not None or port not in (None, 80 if parts.scheme == "http" else 443)):
        raise SearchError("只支持不含账号密码、使用标准端口的公开 HTTP/HTTPS 岗位链接。")
    if "%" in host or host.rstrip(".") == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise SearchError("只能读取公网岗位页面，不能访问本机或内网地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _check_address(address)
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((parts.scheme, authority, quote(parts.path or "/", safe="/%:@-._~!$&'()*+,;="),
                       quote(parts.query, safe="%/:?@-._~!$&'()*+,;="), ""))


def _check_address(address):
    # is_global alone admits multicast in some Python releases.
    if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
        raise SearchError("只能读取公网岗位页面，不能访问本机或内网地址。")


def _public_ip(host: str, port: int) -> str:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not records:
        raise SearchError("无法解析岗位网站地址。")
    for record in records:
        _check_address(ipaddress.ip_address(record[4][0]))
    return records[0][4][0]


def _connect(host: str, port: int, address: str, secure: bool):
    """Pin the checked IP while retaining the hostname for TLS and the Host header.

    Never resolve the original hostname again between validation and connect, and
    never use environment proxies or cookies for arbitrary user-provided URLs.
    """
    connection = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    sock = socket.create_connection((address, port), TIMEOUT)
    try:
        if secure:
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
            sock = context.wrap_socket(sock, server_hostname=host)
        connection.sock = sock
        return connection
    except BaseException:
        sock.close()
        raise


def fetch_html(url: str) -> tuple[str, str]:
    """Fetch bounded HTML with revalidated redirects; return (HTML, final URL)."""
    current = normalize_url(url)
    deadline = time.monotonic() + 30
    try:
        for hop in range(MAX_REDIRECTS + 1):
            if time.monotonic() >= deadline:
                raise TimeoutError()
            parts = urlsplit(current)
            port = 443 if parts.scheme == "https" else 80
            address = _public_ip(parts.hostname, port)
            connection = _connect(parts.hostname, port, address, parts.scheme == "https")
            try:
                target = urlunsplit(("", "", parts.path, parts.query, ""))
                connection.request("GET", target, headers={"Accept": "text/html, application/xhtml+xml",
                                   "Accept-Encoding": "identity", "User-Agent": "Feiran/0.1 (public job reader)"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or hop == MAX_REDIRECTS:
                        raise SearchError("岗位页面重定向过多或缺少目标地址。")
                    current = normalize_url(urljoin(current, location))
                    continue
                if response.status in (401, 403, 429):
                    raise SearchError(f"网站限制自动读取（HTTP {response.status}），可能需要登录或验证码。请手动粘贴 JD。")
                if response.status != 200:
                    raise SearchError(f"岗位页面返回 HTTP {response.status}，请检查链接或手动粘贴 JD。")
                if response.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                    raise SearchError("这个链接未返回 HTML 职位页面，请打开岗位详情页或手动粘贴 JD。")
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding not in ("", "identity", "gzip", "deflate"):
                    raise SearchError("网站未返回可直接读取的网页编码，请手动粘贴 JD。")
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    chunk = response.read1(min(65536, MAX_PAGE_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        raise SearchError("岗位网页过大，请手动粘贴 JD。")
                raw = b"".join(chunks)
                if encoding in ("gzip", "deflate"):
                    try:
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
                        raw = decoder.decompress(raw, MAX_PAGE_BYTES + 1)
                        if len(raw) > MAX_PAGE_BYTES or decoder.unconsumed_tail:
                            raise SearchError("解压后的岗位网页过大，请手动粘贴 JD。")
                        if not decoder.eof or decoder.unused_data:
                            raise SearchError("网页压缩内容不完整或包含额外数据，请手动粘贴 JD。")
                    except zlib.error as exc:
                        raise SearchError("无法解压岗位网页，请手动粘贴 JD。") from exc
                charset = response.headers.get_content_charset()
                if not charset:
                    meta = re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", raw[:8192])
                    charset = meta[1].decode("ascii") if meta else "utf-8"
                try:
                    html = raw.decode(charset)
                except (LookupError, UnicodeError) as exc:
                    raise SearchError("无法正确解码网页文字，请手动粘贴 JD。") from exc
                return html, current
            finally:
                connection.close()
    except TimeoutError as exc:
        raise SearchError("岗位网页读取超时，请稍后重试或手动粘贴 JD。") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise SearchError("无法连接岗位网页或安全读取响应，请稍后重试或手动粘贴 JD。") from exc


def _lines(parts):
    return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())


class JobHTML(HTMLParser):
    """Collect visible body/main text and JSON-LD without running scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body, self.main, self.title, self.heading = [], [], [], []
        self.schemas, self.script = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = ("hidden" in attrs or attrs.get("aria-hidden") == "true" or
                  bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I)))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.script = []
        if tag in BLOCK:
            self.handle_data("\n")
        if tag not in VOID:
            self.stack.append((tag, hidden, attrs.get("role") == "main"))

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.schemas.append("".join(self.script))
            self.script = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break
        if tag in BLOCK:
            self.handle_data("\n")

    def handle_data(self, data):
        tags = {item[0] for item in self.stack}
        if self.script is not None and "script" in tags:
            self.script.append(data)
        if "title" in tags:
            self.title.append(data)
        if tags & SKIP or any(item[1] for item in self.stack) or "head" in tags:
            return
        self.body.append(data)
        if tags & {"main", "article"} or any(item[2] for item in self.stack):
            self.main.append(data)
        if "h1" in tags:
            self.heading.append(data)


def _text(value):
    if not isinstance(value, str):
        return ""
    parser = JobHTML()
    parser.feed(value)
    return _lines(parser.body)


def _job_schemas(value):
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(t in ("JobPosting", "https://schema.org/JobPosting", "http://schema.org/JobPosting") for t in types):
                yield node
            else:
                pending.extend(v for v in node.values() if isinstance(v, (dict, list)))


def extract_job(html: str, source: str) -> dict:
    """Extract one posting, preserving unknown fields and rejecting empty/list pages."""
    parser = JobHTML()
    parser.feed(html)
    jobs = []
    for raw in parser.schemas:
        try:
            jobs.extend(_job_schemas(json.loads(raw)))
        except (ValueError, RecursionError):
            continue
    unique = {json.dumps(job, sort_keys=True): job for job in jobs}
    if len(unique) > 1:
        raise SearchError("页面包含多个岗位，请粘贴单条岗位详情链接。")
    company = location = posted = None
    if unique:
        job = next(iter(unique.values()))
        title = _text(job.get("title"))
        sections = [_text(job.get("description"))]
        for key, label in (("responsibilities", "Responsibilities"), ("qualifications", "Qualifications"),
                           ("skills", "Skills"), ("experienceRequirements", "Experience requirements"),
                           ("educationRequirements", "Education requirements")):
            value = _text(job.get(key))
            if value and value not in sections[0]:
                sections.append(label + "\n" + value)
        text = "\n".join(filter(None, sections))
        organization = job.get("hiringOrganization")
        company = _text(organization.get("name")) if isinstance(organization, dict) else None
        places = job.get("jobLocation", [])
        places = places if isinstance(places, list) else [places]
        locations = []
        for place in places:
            address = place.get("address") if isinstance(place, dict) else None
            if isinstance(address, dict):
                locations.append(", ".join(filter(None, (_text(address.get(k)) for k in
                                                        ("addressLocality", "addressRegion", "addressCountry")))))
            elif isinstance(address, str):
                locations.append(_text(address))
        location = "; ".join(filter(None, locations)) or None
        posted = job.get("datePosted") if isinstance(job.get("datePosted"), str) else None
        method = "jobposting-jsonld"
    else:
        # Document titles are less likely to combine unrelated H1s from banners,
        # navigation and job metadata. They may retain the publisher's site suffix.
        title = _lines(parser.title) or _lines(parser.heading)
        main, body = _lines(parser.main), _lines(parser.body)
        text = main if JOB_SIGNAL.search(main) else body
        method = "html-text"
        if not JOB_SIGNAL.search(text):
            raise SearchError("未读到明确的职位正文；页面可能需要 JavaScript、登录或验证码，也可能不是岗位详情页。请手动粘贴 JD。")
    if not title or len(text) < 80:
        raise SearchError("读取到的职位标题或正文不完整，请手动粘贴 JD。")
    if len(text) > 100_000 or len(title) > 500:
        raise SearchError("页面可能包含多个岗位或大量无关内容，请提供单条岗位链接或手动粘贴 JD。")
    return {"jd": {"title": title, "text": text, "source": source, "provider": "web",
                   "company": company or None, "location": location, "posted_at": posted,
                   "captured_at": datetime.now(timezone.utc).isoformat(), "raw_content": html,
                   "extraction_method": method},
            "source_status": "本次公开网页提取；请核对正文完整性，来源真实性和岗位是否仍开放未核验"}


def fetch_job_url(url: str) -> dict:
    html, final_url = fetch_html(url)
    result = extract_job(html, final_url)
    result["jd"]["requested_url"] = normalize_url(url)
    return result
