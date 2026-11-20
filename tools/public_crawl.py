"""Bounded, same-site HTML crawl for the public demo corpus."""

import collections
import hashlib
import concurrent.futures
import datetime
import json
import threading
import subprocess
from pathlib import Path
import http.client
import ipaddress
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from public_documents import calendar_text, pdf_text

USER_AGENT = "EDU-AI-Demo/1.0"
TOPICS = {
    "financial aid": ("financial-aid", "financial aid", "studentaid", "scholarship", "fafsa"),
    "tuition": ("tuition", "fees", "cost-of-attendance"),
    "admissions": ("admission", "/apply", "application-deadline"),
    "registration": ("registrar", "registration", "enrollment", "academic-calendar"),
    "careers": ("career", "employment", "outcomes"),
    "bookstore": ("bookstore", "bookshop", "student-store", "student store", "campus-store"),
    "dining": ("dining", "restaurant", "food", "where-to-eat", "menu-hours"),
    "locations": ("locations", "visit-us", "visit-campus", "campus-map", "directions", "our-stores"),
    "programs": ("degree", "program", "major", "academics"),
    "services": ("our-services", "our-products", "what-we-do", "services/", "products/"),
    "organization": ("about-us", "/about/", "history", "facts"),
}
SKIP_PATH = re.compile(r"\.(?:jpe?g|gif|svg|png|webp|ico|zip|mp4|css|js|xml|docx?|xlsx?)$|/(?:wp-admin|wp-login|login|logout|archives|feed|search|tag|staff|people)(?:/|$)", re.I)
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK_TAGS = set("address article blockquote br caption dd div dl dt h1 h2 h3 h4 h5 h6 li p section tr".split())


def normalize_url(value):
    value = value.strip()
    if not value or any(ord(c) <= 32 for c in value) or "\\" in value:
        raise ValueError("A valid public website URL is required")
    if "://" not in value:
        value = "https://" + value
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("A public HTTP(S) website is required")
    if url.port not in (None, 443 if url.scheme == "https" else 80):
        raise ValueError("Only standard website ports are supported")
    host = url.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    if len(host) > 253 or "." not in host or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
        raise ValueError("A fully qualified public website name is required")
    path = quote(url.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((url.scheme, host, path, url.query, ""))


def root_host(url):
    host = urlsplit(url).hostname
    return host[4:] if host.startswith("www.") else host


def in_scope(url, root):
    host = urlsplit(url).hostname
    return bool(host) and (host == root or host.endswith("." + root))


class FetchError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class PublicSite:
    def __init__(self, root, *, timeout=6):
        self.root = root
        self.timeout = timeout
        self.robots = {}
        self.lock = threading.Lock()
        self.last_request = {}
        self.tls = ssl.create_default_context()

    def request(self, url):
        """Connect to a validated public address and authenticate the original host via TLS."""
        parts = urlsplit(url)
        if not in_scope(url, self.root):
            raise FetchError("outside the customer website")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global or ipaddress.ip_address(addr[4][0]).is_multicast for addr in addresses):
            raise FetchError("website does not resolve exclusively to public addresses")
        # Pin the DNS result for this request, including each redirect.
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=self.timeout)
        raw_socket = None
        try:
            last_error = None
            for af, kind, proto, _, address in addresses:
                raw_socket = socket.socket(af, kind, proto)
                raw_socket.settimeout(self.timeout)
                try:
                    raw_socket.connect(address)
                    break
                except OSError as exc:
                    last_error = exc
                    raw_socket.close()
            else:
                raise last_error or FetchError("website has no reachable address")
            conn.sock = self.tls.wrap_socket(raw_socket, server_hostname=parts.hostname) if parts.scheme == "https" else raw_socket
            conn.request("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")), headers={"Host": parts.netloc, "User-Agent": USER_AGENT, "Accept": "text/html, application/xml, text/plain, */*;q=0.5", "Connection": "close"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                return response.status, response.getheader("Location"), ""
            if response.status != 200:
                raise FetchError(f"HTTP {response.status}", response.status)
            mime = response.getheader("Content-Type", "").lower()
            if "application/pdf" in mime or parts.path.lower().endswith(".pdf"):
                raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    raise FetchError("PDF exceeds the 12 MB download limit")
                return response.status, raw, "application/pdf"
            limit = 6_000_000 if "calendar" in mime or parts.path.lower().endswith(".ics") else 2_000_000
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise FetchError("source exceeds the download limit")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")
            return response.status, body, response.getheader("Content-Type", "")
        finally:
            conn.close()
            if raw_socket is not None:
                raw_socket.close()

    def fetch(self, url, *, robots_request=False):
        for _ in range(6):
            url = normalize_url(url)
            if not in_scope(url, self.root):
                raise FetchError("redirect leaves the customer website")
            origin = urlsplit(url)
            if not robots_request:
                rules = self.rules(url)
                if not rules.can_fetch(USER_AGENT, url):
                    raise FetchError("excluded by robots.txt")
                delay = max(0.3, rules.crawl_delay(USER_AGENT) or 0)
                rate = rules.request_rate(USER_AGENT)
                if rate and rate.requests:
                    delay = max(delay, rate.seconds / rate.requests)
                if delay > 10:
                    raise FetchError("published crawl delay exceeds demo time budget")
                with self.lock:
                    now = time.monotonic()
                    start = max(now, self.last_request.get(origin.hostname, 0) + delay)
                    self.last_request[origin.hostname] = start
                time.sleep(max(0, start - now))
            else:
                with self.lock:
                    self.last_request.setdefault(origin.hostname, time.monotonic())
            status, body, content_type = self.request(url)
            if status == 200:
                return url, body, content_type
            if not body:
                raise FetchError("redirect has no destination")
            url = urljoin(url, body)
        raise FetchError("too many redirects")

    def rules(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            rules = RobotFileParser()
            try:
                _, body, _ = self.fetch(origin + "/robots.txt", robots_request=True)
                rules.parse(body.splitlines())
            except (FetchError, OSError, http.client.HTTPException, ValueError) as exc:
                if isinstance(exc, FetchError) and exc.status in (404, 410):
                    rules.allow_all = True
                else:
                    rules.disallow_all = True
            self.robots[origin] = rules
        return self.robots[origin]


class PageParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.stack = []
        self.main = []
        self.body = []
        self.title = ""
        self.description = ""
        self.images = []
        self.links = []
        self.headings = []
        self._heading = None

    def add(self, value):
        if self.stack and self.stack[-1][1]:
            return
        self.body.append(value)
        if self.stack and self.stack[-1][2]:
            self.main.append(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict((key, value or "") for key, value in attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(urljoin(self.base_url, attrs["href"]))
        if tag == "meta" and (attrs.get("name") or attrs.get("property", "")).lower() in ("description", "og:description"):
            self.description = attrs.get("content", "")[:800]
        if tag == "img" and attrs.get("src") and "logo" in (attrs.get("alt", "") + attrs["src"]).lower():
            self.images.append(urljoin(self.base_url, attrs["src"]))
        _, skip, main = self.stack[-1] if self.stack else ("", False, False)
        skip = skip or tag in ("script", "style", "noscript", "svg", "header", "footer", "nav", "form", "aside", "template") or attrs.get("role") == "navigation"
        main = main or tag in ("main", "article") or attrs.get("role") == "main"
        if tag in ("h1", "h2", "h3", "h4") and not skip:
            self._heading = []
        if tag not in VOID_TAGS:
            self.stack.append((tag, skip, main))
        if tag in BLOCK_TAGS:
            self.add("\n")

    def handle_endtag(self, tag):
        if self._heading is not None and tag in ("h1", "h2", "h3", "h4"):
            self.headings.append(" ".join(" ".join(self._heading).split()))
            self._heading = None
        if tag in BLOCK_TAGS:
            self.add("\n")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._heading is not None:
            self._heading.append(data)
        if self.stack and self.stack[-1][0] == "title" and not self.stack[-1][1]:
            self.title += data.strip()
        else:
            self.add(data.replace("\n", " "))

    def content(self):
        segments = self.main if len(" ".join(self.main).strip()) >= 120 else self.body
        lines = [" ".join(line.split()) for line in " ".join(segments).split("\n")]
        return "\n".join(dict.fromkeys(line for line in lines if line))[:60_000]


@dataclass
class CrawlResult:
    pages: list = field(default_factory=list)
    logo_url: str = ""
    errors: list = field(default_factory=list)


def topics(url, title=""):
    parts = urlsplit(url)
    name = ((parts.hostname or "") + parts.path + " " + title).lower()
    return [topic for topic, terms in TOPICS.items() if any(term in name for term in terms)]


def sitemap_links(site, start_url):
    maps = list(site.rules(start_url).site_maps() or [urljoin(start_url, "/sitemap.xml")])
    pages = []
    seen = set()
    while maps and len(seen) < 3:
        url = maps.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            _, body, _ = site.fetch(url)
            xml = ET.fromstring(body)
        except (FetchError, OSError, http.client.HTTPException, ValueError, ET.ParseError):
            continue
        links = [el.text.strip() for el in xml.iter() if el.tag.split("}")[-1] == "loc" and el.text]
        if xml.tag.split("}")[-1] == "sitemapindex":
            maps.extend(sorted(links, key=lambda link: ("page" not in link.lower(), len(link)))[:2])
        else:
            pages.extend(links[:2000])
    return pages


def read_source(site, url, hint):
    actual, body, mime = site.fetch(url)
    path = urlsplit(actual).path.lower()
    page = {"url": actual, "description": "", "headings": [], "format": "html"}
    links, images = [], []
    if "application/pdf" in mime:
        doc = pdf_text(body, hint.get("pdf_pages"))
        page.update(doc, format="pdf")
        page["title"] = hint.get("title") or doc["title"]
    elif "calendar" in mime.lower() or path.endswith(".ics"):
        if not isinstance(body, str) or "BEGIN:VCALENDAR" not in body[:500]:
            raise FetchError("not an iCalendar document")
        title, content = calendar_text(body)
        page.update(title=title, text=content, format="ical")
    elif "html" in mime.lower() and isinstance(body, str):
        parser = PageParser(actual)
        parser.feed(body)
        parser.close()
        page.update(title=parser.title[:260] or actual, text=parser.content(), headings=parser.headings, description=parser.description)
        from places import extract_place_markup
        page["place_data"] = extract_place_markup(body, actual)
        links, images = parser.links, parser.images
    else:
        raise FetchError("unsupported document format")
    page["topics"] = sorted(set(topics(actual, page["title"])) | ({hint["topic"]} if hint.get("topic") else set()))
    return page, links, images


def crawl_site(start_url, seed_urls, max_pages):
    if not 1 <= max_pages <= 100:
        raise ValueError("max-pages must be between 1 and 100")
    start_url = normalize_url(start_url)
    root = root_host(start_url)
    site = PublicSite(root)
    result = CrawlResult()
    queued, seen, hashes, listed = {}, set(), set(), set()
    counts, hosts, failures = collections.Counter(), collections.Counter(), collections.Counter()
    deadline = time.monotonic() + 360
    profile = Path(__file__).with_name("source_profiles") / (root + ".json")
    today = datetime.date.today().isoformat()
    hints = {normalize_url(h["url"]): h for h in json.loads(profile.read_text()) if h.get("valid_until", "9999") >= today} if profile.is_file() else {}
    def enqueue(value, priority):
        try:
            url = normalize_url(value)
        except (ValueError, UnicodeError):
            return
        parts = urlsplit(url)
        if not in_scope(url, root) or SKIP_PATH.search(parts.path) or (parts.query and priority < 900) or url in seen:
            return
        # A school's entire document archive can contain years of archived forms. Admit PDFs
        # and feeds from links with topical paths, or from explicit customer/operator seeds.
        if parts.path.lower().endswith((".pdf", ".ics")) and priority < 900 and not topics(url):
            return
        if url in queued:
            queued[url] = max(queued[url], priority)
        elif len(queued) < 3500:
            queued[url] = priority

    enqueue(start_url, 2100)
    for seed in seed_urls:
        url = normalize_url(seed)
        if not in_scope(url, root):
            raise ValueError("Seed URLs must belong to the customer website or one of its subdomains")
        enqueue(url, 2000)
    for i, url in enumerate(hints):
        enqueue(url, 1500-i)
    if root.endswith(".edu") or ".edu." in root or ".ac." in root:
        for prefix in ("catalog", "registrar", "dining", "bookstore", "admissions"):
            enqueue(f"https://{prefix}.{root}/", 65)
    for url in sitemap_links(site, start_url):
        enqueue(url, 8)
    def rank(url):
        parts = urlsplit(url)
        tags = topics(url)
        balance = max(0, 32 - 7 * min((counts[t] for t in tags), default=5))
        host = parts.hostname
        dated = bool(re.search(r"/(?:19|20)\d\d/|/(?:news|event|events|posts?|story)/", parts.path))
        return (queued[url] + balance - (24 if dated else 0) - min(20, hosts[host] * 2)
                - min(15, failures[host] * 5) - parts.path.count("/"), -len(url), url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        pending = {}
        while pending or queued:
            busy = {urlsplit(url).hostname for url in pending.values()}
            launching = time.monotonic() < deadline and len(seen) < max_pages * 6 + 30
            if launching and len(pending) < min(5, max_pages-len(result.pages)):
                for url in sorted(queued, key=rank, reverse=True):
                    if len(pending) >= min(5, max_pages-len(result.pages)):
                        break
                    host = urlsplit(url).hostname
                    if host in busy:
                        continue
                    priority = queued.pop(url)
                    seen.add(url)
                    if failures[host] >= 3 and priority < 900:
                        continue
                    pending[pool.submit(read_source, site, url, hints.get(url, {}))] = url
                    busy.add(host)
            if not pending:
                break
            completed, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in completed:
                url = pending.pop(future)
                host = urlsplit(url).hostname
                try:
                    page, links, images = future.result()
                except (FetchError, OSError, http.client.HTTPException, ValueError, subprocess.SubprocessError) as exc:
                    failures[host] += 1
                    result.errors.append(f"{url}: {exc}")
                    continue
                failures[host] = 0
                actual, content = page["url"], page["text"]
                for link in links:
                    enqueue(link, 20)
                fingerprint = hashlib.sha256(content.encode()).digest()
                if len(content) < 100 or fingerprint in hashes or actual in listed:
                    result.errors.append(f"{url}: duplicate or insufficient source text")
                    continue
                seen.add(actual)
                queued.pop(actual, None)
                listed.add(actual)
                hashes.add(fingerprint)
                hosts[host] += 1
                counts.update(page["topics"])
                result.pages.append(page)
                if not result.logo_url:
                    result.logo_url = next((img for img in images if in_scope(img, root) and urlsplit(img).scheme == "https"), "")
    if not result.pages:
        reason = "; ".join(result.errors[:4]) or "no eligible documents"
        raise ValueError(f"No usable public website content found ({reason}). Supply an accessible official seed URL.")
    return result
