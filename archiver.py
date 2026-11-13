#!/usr/bin/env python3
"""Preserve public VN downloads and provenance, then verify public HF copies."""
from __future__ import annotations

import argparse
import configparser
from contextlib import closing
import hashlib
import hmac
import http.client
import io
from email.message import Message
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import socket
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
import tomllib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPSHandler
import http.cookiejar

HOLY = "https://web.archive.org/web/20181025223313/http://holyseal.net/cgi-bin/newinfo.cgi?mode=file&flt=try"
EXT = re.compile(r"\.(?:zip|exe|rar|7z|lzh|lha|cab|iso|msi|mpg|mpeg|wmv|asf|avi|mp4|mov|webm|mp3|wma|ogg|wav|xp3)(?:$|[?#])", re.I)
WAYBACK = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})(?:[a-z_]+)?/(https?://.*)$")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    md5, sha = hashlib.md5(), hashlib.sha256()
    size = 0
    with Path(path).open("rb") as f:
        while chunk := f.read(1024 * 1024):
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
    return {"md5": md5.hexdigest(), "sha256": sha.hexdigest(), "size": size}


def original(url):
    match = WAYBACK.match(url)
    return match[2] if match else url



def archive_url_identity(url):
    """Compare CDX originals, allowing only equivalent default port spelling."""
    parts=urlsplit(url)
    if parts.scheme not in {'http','https'} or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError('invalid public archive source URL')
    port=parts.port
    if port==({'http':80,'https':443}[parts.scheme]):port=None
    return parts.hostname,port,parts.path,parts.query


def replay(url, timestamp="20181025223313", raw=False, scheme="https"):
    if scheme not in {"http", "https"}:raise ValueError("unsupported archive transport")
    return f"{scheme}://web.archive.org/web/{timestamp}{'id_' if raw else ''}/{original(url)}"


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def resolve(base, href):
    if href.startswith(("javascript:", "mailto:", "data:", "#")):
        return None
    if WAYBACK.match(base):
        stamp = WAYBACK.match(base)[1]
        if href.startswith(("/web/", "https://web.archive.org/", "http://web.archive.org/")):
            target = urljoin(base, href)
            if not WAYBACK.match(target):
                return None
            return replay(original(target), stamp, scheme=urlsplit(base).scheme)
        return replay(urljoin(original(base), href), stamp, scheme=urlsplit(base).scheme)
    result = canonical(urljoin(base, href))
    return result if urlsplit(result).scheme in {"http", "https"} else None


class Node:
    def __init__(self, tag="root", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        # Old publisher HTML can nest thousands of unclosed elements. Preserve
        # the recursive join/strip semantics without Python recursion limits.
        stack, values = [(self, False)], {}
        while stack:
            node, visited = stack.pop()
            if not visited:
                stack.append((node, True))
                stack.extend((child, False) for child in reversed(node.children) if isinstance(child, Node))
            else:
                values[node] = " ".join(values.pop(child) if isinstance(child, Node) else child for child in node.children).strip()
        return values[self]

    def all(self, tags):
        stack = list(reversed(self.children))
        while stack:
            child = stack.pop()
            if isinstance(child, Node):
                if child.tag in tags:
                    yield child
                stack.extend(reversed(child.children))


class DOM(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        # The source sites include old HTML with omitted cell/row end tags.
        if tag in {"tr", "td", "th", "li", "p"}:
            n = self.current
            while n.parent and n.tag not in {tag, "table", "ul", "ol", "body"}:
                n = n.parent
            if n.tag == tag:
                self.current = n.parent
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}:
            self.current = node

    def handle_endtag(self, tag):
        n = self.current
        while n.parent:
            if n.tag == tag:
                self.current = n.parent
                return
            n = n.parent

    def handle_data(self, data):
        if self.current.tag not in {"script", "style"}:
            self.current.children.append(data)


def decode_html(data, charset=None):
    # Script charset attributes describe external scripts, not this document.
    # Replay injections can also push the publisher's declaration past 8 KiB.
    meta = next((match for tag in re.finditer(br"<meta\b[^>]*>", data[:256*1024], re.I)
                 if (match := re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", tag[0], re.I))), None)
    declared = meta[1].decode() if meta else None
    for encoding in dict.fromkeys([charset, declared]):
        if encoding:
            try:
                return data.decode(encoding)
            except (UnicodeError, LookupError):
                pass
    # Wayback inserts UTF-8 toolbar text into legacy-encoded documents. When
    # no strict decoder succeeds, retain publisher text using its declaration.
    if declared:
        try:
            return data.decode(declared, errors="replace")
        except LookupError:
            pass
    # Early mirror pages sometimes omit charset metadata entirely. ISO-2022-JP
    # uses ASCII escape runs, so UTF-8 would otherwise "succeed" with mojibake.
    if (b"\x1b$B" in data or b"\x1b$@" in data) and b"\x1b(B" in data:
        return data.decode("iso2022_jp", errors="replace")
    for encoding in ("utf-8", "cp932", "euc_jp"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    return data.decode("utf-8", errors="replace")


def labels(root):
    fields = {}
    for row in root.all({"tr"}):
        cells = [c for c in row.children if isinstance(c, Node) and c.tag in {"td", "th"}]
        if len(cells) == 2:
            fields[cells[0].text().strip(" :：")] = cells[1].text()
    for paragraph in root.all({"p"}):
        children = [c for c in paragraph.children if isinstance(c, Node)]
        if children and children[0].tag == "strong":
            label = children[0].text()
            fields[label.strip(" :：")] = paragraph.text()[len(label):].strip()
    for dt in root.all({"dt"}):
        siblings = dt.parent.children
        for n in siblings[siblings.index(dt) + 1:]:
            if isinstance(n, Node):
                if n.tag == "dd":
                    fields[dt.text().strip(" :：")] = n.text()
                break
    return fields


def metadata(root):
    fields = labels(root)
    def field(*names):
        return next((v for k, v in fields.items() if k.lower() in names), None)
    md5s = {value.lower() for value in re.findall(r"(?i)\bMD5\b\s*[:：]?\s*([a-f0-9]{32})\b", root.text())}
    exact_sizes = {int(value.replace(',','')) for value in re.findall(r"([\d,]+)\s*(?:bytes?\b|バイト)", root.text(), re.I)}
    return {"brand": field("ブランド", "メーカー", "brand", "maker"),
            "published_at": field("掲載日", "登録日", "公開日", "date"),
            "expected_md5": next(iter(md5s)) if len(md5s)==1 else None,
            "expected_size": next(iter(exact_sizes)) if len(exact_sizes)==1 else None,
            "size_text": field("サイズ", "ファイルサイズ", "size"), "fields": fields}


def parse_detail(html, url, site):
    root = DOM(html).root
    headings = list(root.all({"h1", "h2", "title"}))
    content = next((n for n in root.all({"div"}) if n.attrs.get("id") == "content"), root)
    content_headings = list(content.all({"h1", "h2"}))
    title = content_headings[0].text() if content_headings else (headings[0].text() if headings else url)
    if site == "holyseal" and any({"dltbl", "fixtbl"} & set(n.attrs.get("class", "").split()) for n in root.all({"table"})):
        return parse_holyseal(root, url)
    links = []
    for a in root.all({"a"}):
        target = resolve(url, a.attrs.get("href", ""))
        if not target:
            continue
        raw = original(target)
        if EXT.search(raw) or re.search(r"STORAGE\s*[12]", a.text(), re.I) or "download" in a.attrs:
            links.append((a, target))
    # Group mirrors of the same filename. Never apply a page-wide checksum to
    # several different files: Holyseal product pages may contain many releases.
    groups = {};link_names={}
    for a, target in links:
        name = unquote(Path(urlsplit(original(target)).path).name)
        if not EXT.search(name):
            name = next((v for k, v in labels(root).items() if k in {"ファイル名", "Filename", "filename"}), name or "download.bin")
        groups.setdefault(name, []).append((a, target));link_names[id(a)]=name
    if site == "lathercraft" and not groups:
        filename = labels(root).get("ファイル名")
        if filename:
            # Removed/preparing downloads can still have authoritative metadata.
            # Retain them for recovery from other mirrors rather than dropping them.
            groups[filename] = []
    result = [];page_meta=metadata(root);scope_names={};scope_metadata={}
    for name, candidates in groups.items():
        context = root
        if len(groups) > 1:
            context = candidates[0][0].parent
            while context.parent and context.tag not in {"tr", "li", "article"}:
                context = context.parent
            # If no per-file structure exists, omit ambiguous checksums.
            if context is root:
                context = Node()
        meta = metadata(context)
        if candidates:
            # A malformed old table can nest several releases inside the same
            # row. Only inherit hashes/sizes from a scope containing this one
            # filename, and require agreement across its published mirrors.
            proofs=[]
            for link,target in candidates:
                node=link.parent;proof={'expected_md5':None,'expected_size':None}
                while node is not None:
                    if node not in scope_names:scope_names[node]={link_names[id(child)] for child in node.all({'a'}) if id(child) in link_names}
                    names=scope_names[node]
                    if names!={name}:break
                    if node not in scope_metadata:scope_metadata[node]=metadata(node)
                    local=scope_metadata[node]
                    for key in proof:
                        if proof[key] is None and local[key] is not None:proof[key]=local[key]
                    if all(value is not None for value in proof.values()):break
                    node=node.parent
                proofs.append(proof)
            for key in ['expected_md5','expected_size']:
                values={proof[key] for proof in proofs if proof[key] is not None}
                meta[key]=next(iter(values)) if len(values)==1 else None
        for key in {"brand", "published_at"}:
            meta[key] = meta[key] or page_meta[key]
        mirrors = list(dict.fromkeys(target for _, target in candidates))
        if site == "holyseal":
            mirrors = [replay(u, WAYBACK.match(url)[1], raw=True, scheme=urlsplit(url).scheme) for u in mirrors] + [original(u) for u in mirrors]
        ident = hashlib.sha256((site + "\n" + original(url) + "\n" + name).encode()).hexdigest()
        result.append({"id": ident, "site": site, "page_url": url, "original_page_url": original(url),
                       "title": meta["fields"].get("作品") or title,
                       "release_name": title, "filename": name, "mirrors": mirrors, **meta})
    return result


def parse_holyseal(root, url):
    """Holyseal releases span multiple rows; a mirror row has no checksum itself."""
    heading = next((n.text() for n in root.all({"title"})), "")
    parts = [s.strip() for s in heading.split("≫")]
    title = parts[-1] if len(parts) >= 3 else heading
    brand = parts[-2] if len(parts) >= 3 else None
    product_fields = labels(root)
    stamp = WAYBACK.match(url)[1] if WAYBACK.match(url) else None
    records = []
    for table in root.all({"table"}):
        if not {"dltbl", "fixtbl"} & set(table.attrs.get("class", "").split()):
            continue
        current = None
        for row in table.all({"tr"}):
            strong = next(iter(row.all({"strong"})), None)
            if strong is not None:
                text = row.text()
                cells = [n for n in row.children if isinstance(n, Node) and n.tag in {"td", "th"}]
                file_text = cells[-1].text() if len(cells) >= 2 else text
                # A final parenthesized size is separate from parentheses in
                # a real filename such as tayutama2as_demo(mp4).zip.
                size_match = re.search(r"\s*\(([^()]*)\)\s*$", file_text)
                raw_name = file_text[:size_match.start()].strip() if size_match else file_text.strip()
                name = raw_name if re.search(r"\.[^\s.()]{1,12}$", raw_name) else None
                dates = re.findall(r"\d{4}/\d{2}/\d{2}", text)
                published = dates[-1] if dates else None
                size_text = size_match[1] if size_match else None
                identity = name or f"unknown:{published}:{size_text}"
                current = {"id": hashlib.sha256(("holyseal\n" + original(url) + "\n" + identity + "\n" + strong.text()).encode()).hexdigest(),
                    "site": "holyseal", "page_url": url, "original_page_url": original(url),
                    "title": title, "brand": brand, "release_name": strong.text(), "filename": name,
                    "size_text": size_text, "expected_size": None, "expected_md5": None,
                    "published_at": published, "mirrors": [], "fields": {k: v for k, v in product_fields.items() if k in {"発売日", "ジャンル", "制限等", "OS等", "メディア"}}}
                if name is None:
                    current["fields"]["filename_text"] = raw_name
                records.append(current)
            if current is None:
                continue
            md5 = re.search(r"MD5\s*[:：]\s*([a-f0-9]{32})", row.text(), re.I)
            if md5:
                current["expected_md5"] = md5[1].lower()
            for link in row.all({"a"}):
                target = resolve(url, link.attrs.get("href", ""))
                if target:
                    current["mirrors"].extend(([replay(target, stamp, raw=True)] if stamp else []) + [original(target)])
        for record in records:
            record["mirrors"] = list(dict.fromkeys(record["mirrors"]))
            record["mirrors"].sort(key=lambda u: not bool(WAYBACK.match(u)))
    return records


def source_tls_pins():
    path = Path(__file__).with_name("sources.toml")
    with path.open("rb") as stream:
        pins = tomllib.load(stream).get("tls_pins", {})
    for host, fingerprint in pins.items():
        if host not in {"storage1.lathercraft.net", "storage2.lathercraft.net"} or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("invalid legacy source certificate pin: " + host)
    return pins


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, fingerprint, on_verified, **kwargs):
        # Trust is the exact configured leaf, replacing CA/time validation only
        # for this connection. Compare before sending the HTTP request body.
        kwargs["context"] = ssl._create_unverified_context()
        super().__init__(host, **kwargs)
        self.fingerprint, self.on_verified = fingerprint, on_verified

    def connect(self):
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, self.fingerprint):
            self.close()
            raise ssl.SSLCertVerificationError("legacy source certificate pin mismatch")
        self.on_verified(actual)


class SourceHTTPSHandler(HTTPSHandler):
    def __init__(self, pins, on_verified):
        super().__init__()
        self.pins, self.on_verified = pins, on_verified

    def https_open(self, req):
        def connection(host, **kwargs):
            # urllib may connect through an HTTP proxy. Pin the tunnel target,
            # and reselect trust on every redirect rather than reusing it.
            target = urlsplit("//" + (req._tunnel_host or host)).hostname
            if target in self.pins:
                return PinnedHTTPSConnection(host, fingerprint=self.pins[target], on_verified=self.on_verified, **kwargs)
            return http.client.HTTPSConnection(host, **kwargs)
        return self.do_open(connection, req, context=self._context)


def retry_after_delay(value, minimum):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 0
    return max(minimum, delay)


class Fetcher:
    def __init__(self, delay=1, timeout=45, retries=3, tls_pins=None):
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.last = 0.0
        self.not_before = 0.0
        self.tls_pins, self.last_tls_pin = dict(tls_pins or {}), None
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()), SourceHTTPSHandler(self.tls_pins, self._record_pin))

    def _record_pin(self, fingerprint):
        self.last_tls_pin = fingerprint

    def open(self, url, headers=None):
        self.last_tls_pin = None
        parts=urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError("only HTTP(S) URLs are supported")
        if re.search(r"[\x00-\x1f\x7f]",url):
            raise ValueError("control characters in source URL")
        # Publishers sometimes leave spaces or Japanese characters unescaped.
        # Encode only the wire URL; source metadata and existing escapes remain.
        transport_url=urlunsplit((parts.scheme,parts.netloc,
            quote(parts.path,safe="/%:@!$&'()*+,;=-._~"),
            quote(parts.query,safe="%/?@:!$&'()*+,;=-._~"),""))
        for attempt in range(self.retries):
            current = time.monotonic()
            time.sleep(max(0, self.delay - (current - self.last), self.not_before - current))
            self.last = time.monotonic()
            try:
                return self.opener.open(Request(transport_url, headers={"User-Agent": "VN-Mirror-Archiver/0.1 (cultural preservation)",
                    "Accept-Encoding": "identity", **(headers or {})}), timeout=self.timeout)
            except HTTPError as e:
                if e.code not in {408, 429, 500, 502, 503, 504}:
                    raise
                delay = retry_after_delay(e.headers.get("Retry-After", ""), 60 if e.code == 429 else 2 ** attempt)
                # A cooldown also applies to the next URL if this request uses
                # its last retry; otherwise a crawler immediately hits the host again.
                self.not_before = max(self.not_before, time.monotonic() + delay)
                if e.code == 429:
                    print(f"rate limited: waiting {delay:.0f}s before retrying {urlsplit(url).hostname}", file=sys.stderr, flush=True)
                e.close()
                if attempt + 1 == self.retries:
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt + 1 == self.retries:
                    raise
                time.sleep(2 ** attempt)


def open_archive_index(fetcher, url):
    """Use the public HTTP CDX endpoint only if its HTTPS port is refused."""
    parts=urlsplit(url)
    if parts.netloc!="web.archive.org" or parts.scheme not in {"https","http"} or parts.path!="/cdx/search/cdx":
        raise ValueError("not a Wayback CDX endpoint")
    try:
        return fetcher.open(url)
    except (URLError,ConnectionRefusedError) as error:
        reason=error.reason if isinstance(error,URLError) else error
        if parts.scheme!="https" or not isinstance(reason,ConnectionRefusedError):raise
        return fetcher.open(urlunsplit(("http",parts.netloc,parts.path,parts.query,"")))



def open_archive_page(fetcher, url, headers=None):
    """Retry the same Wayback HTML capture on its other public transport."""
    parts=urlsplit(url)
    try:
        return fetcher.open(url, headers)
    except (URLError,ConnectionRefusedError) as error:
        reason=error.reason if isinstance(error,URLError) else error
        if (parts.scheme not in {'http','https'} or parts.netloc!='web.archive.org'
                or not re.match(r'^/web/\d{14}(?:id_|if_|fw_)?/',parts.path)
                or not isinstance(reason,ConnectionRefusedError)):
            raise
        return fetcher.open(urlunsplit(('http' if parts.scheme=='https' else 'https',parts.netloc,parts.path,parts.query,'')),headers)


def read_archive_page(fetcher, url, headers=None):
    """Read publisher HTML behind Wayback's same-capture playback wrapper."""
    seen=set()
    for _ in range(3):
        if url in seen:raise ValueError('cyclic Wayback playback frame')
        seen.add(url)
        with open_archive_page(fetcher,url,headers) as response:
            actual=canonical(response.geturl());raw=response.read(16*1024*1024+1)
            charset=response.headers.get_content_charset()
            fetched_at=getattr(response,'fetched_at',None) or now()
        if len(raw)>16*1024*1024:raise ValueError('HTML exceeds 16 MiB')
        capture=WAYBACK.match(actual)
        frame=next((n for n in DOM(decode_html(raw,charset)).root.all({'iframe'}) if n.attrs.get('id')=='playback'),None) if capture else None
        if frame is None:return raw,actual,charset,fetched_at
        target=urljoin(actual,frame.attrs.get('src',''));embedded=WAYBACK.match(target)
        # Ordinary publisher iframes are not Wayback wrappers. Never follow a
        # different host, capture date or original URL under this special rule.
        try:
            same=(bool(frame.attrs.get('src')) and urlsplit(actual).netloc=='web.archive.org' and urlsplit(target).netloc=='web.archive.org'
                  and re.match(r'^/web/\d{14}(?:id_|if_|fw_)/',urlsplit(target).path)
                  and embedded and embedded[1]==capture[1]
                  and archive_url_identity(original(target))==archive_url_identity(original(actual)))
        except ValueError:same=False
        if not same:return raw,actual,charset,fetched_at
        url=target
    raise ValueError('Wayback playback frame depth exceeds bound')


def archive_index_scheme(store,target_url,index_url=None):
    """Retain the actual public CDX transport when deriving replay URLs."""
    if index_url is not None:
        parts=urlsplit(index_url)
        if parts.netloc=='web.archive.org' and parts.path=='/cdx/search/cdx' and parts.scheme in {'http','https'}:
            store.db.execute('INSERT OR REPLACE INTO archive_index_transports VALUES(?,?,?)',(target_url,index_url,now()))
            return parts.scheme
    saved=store.db.execute('SELECT index_url FROM archive_index_transports WHERE target_url=?',(target_url,)).fetchone()
    return urlsplit(saved[0]).scheme if saved else 'https'


class CachedPageFetcher:
    """Resume a collection from saved successful HTML without refetching it."""
    def __init__(self, store, fetcher):
        self.store, self.fetcher = store, fetcher

    def open(self, url, headers=None):
        row = self.store.db.execute("SELECT * FROM pages WHERE url=?", (canonical(url),)).fetchone()
        recoverable = row and (row["status"] in {"ok", "wayback_wrapper"} or (row["error"] or "").startswith(("no downloadable links", "Holyseal release has no recognized filename")))
        if not headers and recoverable and row["path"] and (self.store.root / row["path"]).is_file():
            response = io.BytesIO((self.store.root / row["path"]).read_bytes())
            response.status = 200
            response.headers = Message()
            response.geturl = lambda: row["response_url"] or url
            response.fetched_at = row["fetched_at"]
            return response
        return self.fetcher.open(url, headers)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        from archive_lock import exclusive_file
        # WAL mode changes and additive migrations race on a fresh database,
        # even with SQLite busy_timeout. Serialize setup across processes while
        # leaving ordinary per-connection reads/transfers concurrent.
        with exclusive_file(self.root / ".sqlite-init.lock"):
            try:
                self._initialize()
            except BaseException:
                if hasattr(self,"db"):self.db.close()
                raise

    def _initialize(self):
        self.db = sqlite3.connect(self.root / "index.sqlite3", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, site TEXT NOT NULL, data TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', path TEXT, md5 TEXT, sha256 TEXT, size INTEGER,
          source_url TEXT, fetched_at TEXT, error TEXT, remote_key TEXT, remote_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, site TEXT, path TEXT, fetched_at TEXT,
          status TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, item_id TEXT, url TEXT,
          attempted_at TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS attempts_item ON attempts(item_id,id);
        CREATE INDEX IF NOT EXISTS items_expected_md5 ON items(json_extract(data,'$.expected_md5'));
        CREATE TABLE IF NOT EXISTS archive_index_transports(target_url TEXT PRIMARY KEY,index_url TEXT NOT NULL,checked_at TEXT NOT NULL);
        """)
        from catalog import initialize
        initialize(self)
        from size_reviews import initialize as initialize_reviews
        initialize_reviews(self.db)

    def reviewed_record(self, record):
        from size_reviews import apply
        return apply(self, record)

    def preserve_public_version(self, row):
        """Snapshot fully verified public bytes inside the caller's transaction."""
        sha=row['sha256']
        if (not sha or row['remote_sha256']!=sha or not row['remote_verified_at']
                or not row['remote_key'] or not row['remote_key'].endswith('/files/'+sha)):
            return None
        ident=hashlib.sha256(('verified-history\n'+row['id']+'\n'+sha).encode()).hexdigest()
        data=json.loads(row['data']);data['id']=ident
        data['fields']={**(data.get('fields') or {}),'verified_history_of':row['id'],'verified_history_sha256':sha}
        saved=dict(row);saved.update(id=ident,data=json.dumps(data,ensure_ascii=False),status='archived',path=None)
        columns=list(saved)
        changed=self.db.execute('INSERT OR IGNORE INTO items('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(saved.values())).rowcount
        if changed:
            from catalog import index_record
            index_record(self,data)
        return ident

    def add(self, record):
        record = self.reviewed_record(record)
        old = self.db.execute("SELECT * FROM items WHERE id=?", (record["id"],)).fetchone()
        from catalog import index_record
        with self.db:
            if old and json.loads(old['data']) != record:
                previous = json.loads(old['data'])
                same_file = (previous.get("filename") == record.get("filename")
                    and previous.get("mirrors") == record.get("mirrors"))
                retained = (old["status"] in {"verified", "archived"} and old["sha256"]
                    and (not record.get("expected_md5") or record["expected_md5"] == old["md5"])
                    and (not record.get("expected_sha256") or record["expected_sha256"] == old["sha256"])
                    and (record.get("expected_size") is None or record["expected_size"] == old["size"])
                    and (record.get("expected_md5") or record.get("expected_sha256") or same_file))
                if not retained:
                    history=self.preserve_public_version(old)
                    if history:
                        # Old verified bytes stay independently downloadable.
                        # The changed observation has no measured bytes yet.
                        self.db.execute("UPDATE items SET status='pending',path=NULL,md5=NULL,sha256=NULL,size=NULL,source_url=NULL,fetched_at=NULL,error=NULL,remote_key=NULL,remote_sha256=NULL,remote_verified_at=NULL,reused_from=NULL WHERE id=?",(record['id'],))
                    else:
                        self.db.execute("UPDATE items SET status='pending',remote_sha256=NULL WHERE id=?", (record["id"],))
            self.db.execute("INSERT INTO items(id,site,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (record["id"], record["site"], json.dumps(record, ensure_ascii=False)))
            index_record(self, record)

    def update(self, ident, **kwargs):
        self.db.execute("UPDATE items SET " + ",".join(k + "=?" for k in kwargs) + " WHERE id=?", (*kwargs.values(), ident))
        self.db.commit()

    def update_if_current(self, ident, data, **kwargs):
        changed = self.db.execute("UPDATE items SET " + ",".join(k + "=?" for k in kwargs) + " WHERE id=? AND data=?", (*kwargs.values(), ident, data)).rowcount
        self.db.commit()
        return bool(changed)

    def export(self):
        from catalog import export_manifest
        return export_manifest(self)

    def rows(self, site="both"):
        return self.db.execute("SELECT i.* FROM items i JOIN item_catalog c ON c.item_id=i.id WHERE (?='both' OR site=?) ORDER BY CASE c.kind WHEN 'trial' THEN 0 WHEN 'patch' THEN 1 WHEN 'full' THEN 2 WHEN 'movie' THEN 3 ELSE 4 END,i.id", (site, site)).fetchall()


def is_detail(url, site):
    p = urlsplit(original(url))
    if site == "lathercraft":
        return bool(re.fullmatch(r"/items/[^/]+", p.path))
    return p.path.endswith("mlistview.cgi") and "prdcode" in parse_qs(p.query)


def crawl(store, fetcher, site, seed, max_pages=None, max_items=None, export=True):
    queue, seen, details = deque([seed]), set(), set()
    list_count = item_count = failures = 0
    seed_host = urlsplit(original(seed)).netloc
    while queue:
        url = canonical(queue.popleft())
        if url in seen:
            continue
        seen.add(url)
        detail = is_detail(url, site)
        if detail:
            if max_items is not None and item_count >= max_items:
                continue
            item_count += 1
        else:
            if max_pages is not None and list_count >= max_pages:
                continue
            list_count += 1
        rel = "html/" + hashlib.sha256(url.encode()).hexdigest() + ".html"
        effective_url = None
        try:
            data,effective_url,charset,fetched_at=read_archive_page(fetcher,url)
            if site == "holyseal":
                final = WAYBACK.match(effective_url)
                if not final or urlsplit(original(effective_url)).hostname != urlsplit(original(seed)).hostname:
                    raise ValueError("Wayback redirected outside the source site")
            html = decode_html(data,charset)
            target = store.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)", (url, site, rel, fetched_at, "ok", None, effective_url))
            root = DOM(html).root
            found = []
            for a in root.all({"a"}):
                link = resolve(effective_url, a.attrs.get("href", ""))
                if not link or urlsplit(original(link)).netloc != seed_host:
                    continue
                if is_detail(link, site):
                    found.append(link)
                elif not detail:
                    p = urlsplit(original(link))
                    if site == "lathercraft" and p.path.rstrip("/") == "/items" and "next" in a.attrs.get("rel", "").split():
                        queue.append(link)
                    elif site == "holyseal" and p.path == urlsplit(original(seed)).path:
                        # Keep the original content filter but do not assume a page-number parameter.
                        params, seed_params = parse_qs(p.query), parse_qs(urlsplit(original(seed)).query)
                        if all(params.get(k) == v for k, v in seed_params.items() if k in {"mode", "flt"}):
                            queue.append(link)
            if detail:
                records = parse_detail(html, effective_url, site)
                from mirror_sources import add_record
                for record in records:
                    add_record(store, record)
                if not records:
                    raise ValueError("no downloadable links recognized; saved HTML needs parser review")
                print(f"{site}: {len(records)} file(s) {url}")
            else:
                fresh = [u for u in found if u not in details]
                if not found:
                    raise ValueError("no detail links recognized; refusing to report an empty crawl as success")
                details.update(found)
                queue.extendleft(reversed(fresh))
        except (OSError, ValueError, http.client.HTTPException) as e:
            failures += 1
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)", (url, site, rel if (store.root / rel).exists() else None, now(), "failed", str(e), effective_url))
            print(f"{site}: failed {url}: {e}", file=sys.stderr)
        store.db.commit()
    if export:
        store.export()
    return failures


def validate(path, record, previous=None):
    result = digest(path)
    if not result["size"]:
        raise ValueError("empty file")
    if record.get("expected_md5") and result["md5"] != record["expected_md5"]:
        raise ValueError("MD5 mismatch")
    if record.get("expected_sha256") and result["sha256"] != record["expected_sha256"]:
        raise ValueError("SHA-256 mismatch")
    if record.get("expected_size") is not None and result["size"] != record["expected_size"]:
        raise ValueError("published file size mismatch")
    if previous and previous.get("sha256") and result["sha256"] != previous["sha256"]:
        raise ValueError("SHA-256 mismatch against local manifest")
    with Path(path).open("rb") as f:
        head = f.read(512).lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ValueError("HTML error page returned instead of a file")
    return result


def transfer(fetcher, url, destination, record):
    from drive_download import open_download
    # Partial bytes belong to one observed URL. Resumption requires either an
    # HTTP validator or an independent expected digest for the complete file.
    suffix = hashlib.sha256(url.encode()).hexdigest()[:16]
    part = destination.with_name(destination.name + "." + suffix + ".part")
    state_path = part.with_suffix(".json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    bound=state.get('url')==url
    expected_digest=record.get('expected_md5') or record.get('expected_sha256')
    if bound and part.is_file() and expected_digest:
        try:completed=validate(part,record)
        except ValueError:completed=None
        if completed:
            part.replace(destination);state_path.unlink(missing_ok=True)
            return completed
    offset = part.stat().st_size if part.exists() and bound and (state.get("validator") or expected_digest) else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    if offset and state.get('validator'):headers['If-Range']=state['validator']
    # Public RetroPC downloads document ordinary same-site Referer navigation.
    # Keep this scoped to their published product pages and binary directory.
    source = record.get("original_page_url", "")
    if (record.get("site") == "alice_archives" and urlsplit(url).hostname == "retropc.net"
            and urlsplit(url).path.startswith("/alice/files/")
            and urlsplit(source).hostname == "retropc.net"
            and re.fullmatch(r"/alice/\d+\.html", urlsplit(source).path)):
        headers["Referer"] = source
    try:
        response = open_download(fetcher, url, headers)
    except HTTPError as e:
        if e.code != 416 or not offset:
            raise
        # Even an unsatisfied range is not proof of completion. Restart and
        # verify the full response rather than accepting a truncated .part.
        e.close()
        offset = 0
        response = open_download(fetcher, url, {k:v for k,v in headers.items() if k not in {"Range", "If-Range"}} or None)
    with response:
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("unexpected content encoding for binary transfer")
        if "html" in response.headers.get("Content-Type", "").lower():
            raise ValueError("server returned HTML for a download")
        total = None
        if response.status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[2]) >= int(match[3]):
                raise ValueError("invalid Content-Range")
            total = int(match[3])
            if offset and state.get("validator") and response.headers.get("ETag", response.headers.get("Last-Modified")) != state["validator"]:
                raise ValueError("validator changed during resume")
        elif response.status == 200:
            offset = 0  # Server ignored Range, or If-Range forced a full response.
        else:
            raise ValueError(f"unexpected download status {response.status}")
        expected_body = response.headers.get("Content-Length")
        advertised_total = total if total is not None else int(expected_body) if expected_body is not None else None
        if (record.get("expected_size") is not None and advertised_total is not None
                and advertised_total != record["expected_size"]):
            # Reject a known different build before consuming its body. Keep
            # existing URL-bound partial bytes and their validator untouched.
            raise ValueError("published file size mismatch (HTTP metadata)")
        validator = response.headers.get("ETag")
        if not validator or validator.startswith("W/"):
            validator = response.headers.get("Last-Modified")
        state_path.write_text(json.dumps({"url": url, "validator": validator}))
        count = 0
        with part.open("ab" if offset else "wb") as f:
            while chunk := response.read(1024 * 1024):
                reserve = getattr(fetcher, "reserve_bytes", 0)
                if reserve and shutil.disk_usage(destination.parent).free < reserve + len(chunk):
                    raise OSError("disk reserve reached; resumable partial preserved")
                f.write(chunk)
                count += len(chunk)
        if expected_body is not None and count != int(expected_body):
            raise ValueError("truncated HTTP body")
        if total is not None and part.stat().st_size != total:
            raise ValueError("incomplete range transfer")
    try:
        result = validate(part, record)
    except ValueError:
        part.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        raise
    part.replace(destination)
    state_path.unlink(missing_ok=True)
    return result


def matching_mirrors(store, row, record):
    """Recover observed URLs from any same-maker published MD5 association.

    Original source metadata remains unchanged. Actual selected URLs and every
    failed attempt are recorded by download; all bytes still pass validate().
    """
    if not record.get("expected_md5") or not record.get("brand"):
        return []
    candidates = store.db.execute("""SELECT i.id,i.data FROM items i
        JOIN item_catalog c ON c.item_id=i.id JOIN products p ON p.id=c.product_id
        WHERE i.id!=? AND json_extract(i.data,'$.expected_md5')=?
        AND p.manufacturer_id=(SELECT p2.manufacturer_id
        FROM item_catalog c2 JOIN products p2 ON p2.id=c2.product_id WHERE c2.item_id=?)""",
        (row["id"],record["expected_md5"],row["id"]))
    mirrors = []
    for candidate in candidates:
        donor = json.loads(candidate['data'])
        if (record.get("expected_size") is not None and donor.get("expected_size") is not None
                and record["expected_size"] != donor["expected_size"]):
            continue
        if (record.get("expected_sha256") and donor.get("expected_sha256")
                and record['expected_sha256']!=donor['expected_sha256']):
            continue
        # These are indexed captures of the donor's actual published URLs,
        # never a guessed path or a capture date borrowed from another file.
        mirrors.extend(r[0] for r in store.db.execute(
            'SELECT url FROM wayback_candidates WHERE item_id=? ORDER BY captured_at DESC',(candidate['id'],)))
        mirrors.extend(donor["mirrors"])
    return sorted(dict.fromkeys(mirrors),key=lambda u:urlsplit(u).hostname!='storage1.lathercraft.net')


def recover_completed_transfer(store,row,record,path,rel):
    """Recover a completed, checksum-bound transfer that lost its metadata CAS."""
    if (not (record.get('expected_md5') or record.get('expected_sha256'))
            or path.is_symlink() or not path.is_file()):return False
    receipt=store.db.execute("""SELECT url,attempted_at FROM attempts WHERE item_id=?
        AND error LIKE 'source metadata changed during transfer%' ORDER BY id DESC LIMIT 1""",(row['id'],)).fetchone()
    if not receipt:return False
    try:result=validate(path,record)
    except (OSError,ValueError):return False
    if not store.update_if_current(row['id'],row['data'],status='verified',path=rel,source_url=receipt['url'],
            fetched_at=receipt['attempted_at'],error=None,remote_sha256=None,**result):return False
    print(f"recovered completed transfer {rel} ({result['size']} bytes)",flush=True)
    return True


def download(store, fetcher, site="both", limit=None, ident=None, export=True):
    failures = processed = 0
    if ident is not None and not any(row["id"] == ident for row in store.rows(site)):
        raise ValueError("source record ID not found for selected site: " + ident)
    for row in store.rows(site):
        if ident is not None and row["id"] != ident:
            continue
        record = json.loads(row["data"])
        if row["status"] == "archived" and row["sha256"] and row["remote_sha256"] == row["sha256"] and row["remote_verified_at"]:
            continue
        source_fetcher = fetcher
        # Exact certificate pins authenticate the configured source independently
        # of optional publisher digests. Some real entries publish no MD5 at all;
        # retain that absence and still record actual size, MD5 and SHA-256.
        if row["status"] == "verified":
            try:
                validate(store.root / row["path"], record, dict(row))
                continue
            except (OSError, ValueError):
                store.update(row["id"], status="pending", remote_sha256=None)
        if limit is not None and processed >= limit:
            break
        processed += 1
        name = re.sub(r"[^\w.() -]", "_", (record["filename"] or row["id"]))[:150].strip(". ") or "download.bin"
        rel = f"files/{row['site']}/{row['id']}/{name}"
        path = store.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if recover_completed_transfer(store,row,record,path,rel):continue
        captures = [r[0] for r in store.db.execute("SELECT url FROM wayback_candidates WHERE item_id=? ORDER BY captured_at DESC", (row["id"],))]
        recovered = matching_mirrors(store, row, record)
        primary = [u for u in recovered if urlsplit(u).hostname=='storage1.lathercraft.net']
        fallback = [u for u in recovered if u not in primary]
        mirrors = list(dict.fromkeys(primary + captures + record["mirrors"] + fallback))
        if not mirrors:
            store.update(row["id"], status="failed", error="No mirror link preserved on source page")
        if record["site"] == "lathercraft":
            # storage2 repeatedly timed out in live collection; try the working
            # primary first without changing the source's recorded mirror list.
            mirrors = sorted(mirrors, key=lambda u: urlsplit(u).hostname != "storage1.lathercraft.net")
        for url in mirrors:
            error = None
            try:
                result = transfer(source_fetcher, url, path, record)
                acknowledged=store.update_if_current(row["id"], row["data"], status="verified", path=rel, source_url=url, fetched_at=now(), error=None, remote_sha256=None, **result)
                if not acknowledged:
                    current=store.db.execute('SELECT data FROM items WHERE id=?',(row['id'],)).fetchone()
                    fresh=json.loads(current['data']) if current else {}
                    # Descriptive/source-page changes must not discard a
                    # completed build independently authenticated by the current
                    # publisher checksum. Every current constraint is rechecked.
                    if fresh.get('expected_md5') or fresh.get('expected_sha256'):
                        try:validated=validate(path,fresh)
                        except ValueError:validated=None
                        if validated:
                            acknowledged=store.update_if_current(row['id'],current['data'],status='verified',path=rel,source_url=url,fetched_at=now(),error=None,remote_sha256=None,**validated)
                    if not acknowledged:
                        raise ValueError("source metadata changed during transfer; retry against current expectations")
                print(f"verified {rel} ({result['size']} bytes)")
            except (OSError, ValueError, http.client.HTTPException) as e:
                error = str(e)
                # Try the exact same capture if one public Wayback port is
                # refused. Upgrading to HTTPS retains TLS authentication; an
                # HTTP downgrade additionally requires a publisher checksum.
                # HTTP denials and certificate errors never trigger this retry.
                reason = e.reason if isinstance(e, URLError) else e
                if (isinstance(reason, ConnectionRefusedError) and WAYBACK.match(url)
                        and (urlsplit(url).scheme == "http" or record.get("expected_md5") or record.get("expected_sha256"))):
                    alternate = ("https://" if urlsplit(url).scheme == "http" else "http://") + urlsplit(url).netloc + urlsplit(url).path
                    if urlsplit(url).query:alternate += "?" + urlsplit(url).query
                    if alternate not in mirrors:mirrors.insert(mirrors.index(url)+1,alternate)
                store.update_if_current(row["id"], row["data"], status="failed", error=error)
                print(f"download failed {url}: {error}", file=sys.stderr)
            store.db.execute("INSERT INTO attempts(item_id,url,attempted_at,error,tls_pin) VALUES(?,?,?,?,?)", (row["id"], url, now(), error, getattr(source_fetcher, "last_tls_pin", None)))
            store.db.commit()
            if error is None:
                break
        else:
            failures += 1
    if export:
        store.export()
    return failures


def verify(store, site="both"):
    failures = 0
    for row in store.rows(site):
        if not row["path"]:
            continue
        try:
            result = validate(store.root / row["path"], json.loads(row["data"]), dict(row))
            store.update(row["id"], status="verified", error=None, **result)
        except (OSError, ValueError) as e:
            failures += 1
            store.update(row["id"], status="failed", error=str(e), remote_sha256=None)
            print(f"verify failed {row['id']}: {e}", file=sys.stderr)
    store.export()
    return failures


def s3_client(namespace, profile="hf"):
    import boto3
    from botocore.config import Config
    config = Config(region_name="us-east-1", s3={"addressing_style": "path"},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required",
                    connect_timeout=15, read_timeout=120, retries={"max_attempts": 3})
    session = boto3.Session(profile_name=profile)
    return session.client("s3", endpoint_url=f"https://{namespace}.s3.hf.co", config=config)


def public_url(namespace, bucket, key, download=False):
    url = f"https://huggingface.co/buckets/{quote(namespace, safe='')}/{quote(bucket, safe='')}/resolve/{quote(key, safe='/')}"
    return url + ("?download=true" if download else "")


def public_digest(namespace, bucket, key):
    sha, size = hashlib.sha256(), 0
    with Fetcher(delay=0).open(public_url(namespace, bucket, key)) as response:
        while chunk := response.read(1024 * 1024):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def init_bucket(namespace, bucket, profile="hf"):
    from botocore.exceptions import ClientError
    client = s3_client(namespace, profile)
    try:
        client.create_bucket(Bucket=bucket)
        print(f"created {namespace}/{bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
        print(f"already owned: {namespace}/{bucket}")
    # Public binary read-back is validated during actual file publication.
    # Bucket initialization does not leave documentation or test objects behind.
    client.head_bucket(Bucket=bucket)
    print(f"bucket ready: https://huggingface.co/buckets/{namespace}/{bucket}")


def publish(store, namespace, bucket, profile="hf", site="both", limit=None, ident=None, metadata=True, recheck=False):
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import ClientError
    client = s3_client(namespace, profile)
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as e:
        if str(e.response["Error"]["Code"]) not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=bucket)
    config = TransferConfig(multipart_threshold=256 * 1024**2, multipart_chunksize=256 * 1024**2, max_concurrency=2)
    count = 0
    checked = {}
    for row in store.rows(site):
        if ident is not None and row["id"] != ident:
            continue
        if row["status"] not in {"verified", "archived"}:
            continue
        expected_key = f"{namespace}/{bucket}/files/{row['sha256']}"
        published = row["sha256"] and row["remote_sha256"] == row["sha256"] and row["remote_verified_at"] and row["remote_key"] == expected_key
        if published and not recheck:
            continue
        if limit is not None and count >= limit:
            break
        path = store.root / row["path"] if row["path"] else None
        result = validate(path, json.loads(row["data"]), dict(row)) if path else {"sha256": row["sha256"], "size": row["size"]}
        key = "files/" + result["sha256"]
        count += 1
        # A successful upload alone is insufficient. Always anonymously read
        # back the complete object, including on a rerun after interruption.
        known = store.db.execute("SELECT 1 FROM items WHERE remote_key=? AND remote_sha256=? AND remote_verified_at IS NOT NULL", (f"{namespace}/{bucket}/{key}", result["sha256"])).fetchone()
        if key not in checked and not known:
            if path is None:
                raise ValueError("local file unavailable for unpublished object: " + row["id"])
            client.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": result["sha256"]}}, Config=config)
        remote_sha, remote_size = checked.get(key) or public_digest(namespace, bucket, key)
        if (remote_sha, remote_size) != (result["sha256"], result["size"]):
            store.update(row["id"], remote_sha256=None, remote_verified_at=None)
            raise ValueError(f"public read-back mismatch: {key}")
        checked[key] = remote_sha, remote_size
        if not store.update_if_current(row["id"], row["data"], remote_key=f"{namespace}/{bucket}/{key}", remote_sha256=remote_sha, remote_verified_at=now()):
            raise ValueError("source metadata changed during publication; verified remote object retained")
        print(f"public read-back verified: {namespace}/{bucket}/{key}")
    if metadata:
        publish_metadata(store, namespace, bucket, profile)
    print(f"published {count} file(s); https://huggingface.co/buckets/{namespace}/{bucket}")
    return count


def publish_metadata(store, namespace, bucket, profile="hf", include_html=True):
    from archive_lock import exclusive_file
    # Slow provenance uploads must not hold the mutable catalogue checkpoint
    # lock. Acquisition batches only checkpoint SQLite; a separate worker drains
    # saved HTML with its own lock and per-object public verification.
    if include_html:
        with exclusive_file(store.root / ".provenance.lock"):
            _publish_html(store, namespace, bucket, profile)
    with exclusive_file(store.root / ".publish.lock"):
        _publish_snapshot(store, namespace, bucket, profile)


def _publish_html(store, namespace, bucket, profile="hf"):
    from boto3.s3.transfer import TransferConfig
    client = s3_client(namespace, profile)
    config = TransferConfig(multipart_threshold=256 * 1024**2, multipart_chunksize=256 * 1024**2, max_concurrency=2)
    # Publish provenance and saved HTML, never credentials, SQLite journals,
    # partial files, or arbitrary files from the workspace.
    provenance = [store.root / r[0] for r in store.db.execute("SELECT path FROM pages WHERE path IS NOT NULL")]
    known = {r[0]: r[1] for r in store.db.execute("SELECT key,sha256 FROM published_objects")}
    paths = iter(dict.fromkeys(provenance))

    def send(path):
        key = path.relative_to(store.root).as_posix()
        identity = f"{namespace}/{bucket}/{key}"
        # Stage mutable source HTML before hashing and sending it.
        # Upload an immutable staging copy and verify those exact bytes.
        with tempfile.TemporaryDirectory(prefix="mirror-provenance-") as tmp:
            staged = Path(tmp) / "artifact"
            shutil.copyfile(path, staged)
            expected = digest(staged)
            if known.get(identity) == expected["sha256"]:
                return None
            client.upload_file(str(staged), bucket, key, Config=config)
            if public_digest(namespace, bucket, key) != (expected["sha256"], expected["size"]):
                raise ValueError(f"public provenance read-back mismatch: {key}")
            return identity, expected["sha256"], expected["size"], now()

    # Boto clients support shared use by threads. Only this main thread writes
    # SQLite, and at most four remote objects are in flight at a time.
    count = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = set()
        for _ in range(4):
            path = next(paths, None)
            if path is not None:
                pending.add(pool.submit(send, path))
        try:
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    if result:
                        store.db.execute("INSERT OR REPLACE INTO published_objects VALUES(?,?,?,?)", result)
                        store.db.commit()
                        count += 1
                        if count % 50 == 0:
                            print(f"metadata: {count} new objects verified", flush=True)
                    path = next(paths, None)
                    if path is not None:
                        pending.add(pool.submit(send, path))
        except BaseException:
            for future in pending:
                future.cancel()
            raise


def _compact_snapshot(db):
    # This is an offline backup only. The live crawl keeps its existing schema.
    # Link primary keys contain long URLs; a rowid table duplicates those bytes
    # in its primary-key index (about 246 MB in the current catalogue).
    schema=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='mirror_links'").fetchone()
    columns=[row[1] for row in db.execute("PRAGMA table_info(mirror_links)")]
    if (schema and "WITHOUT ROWID" not in schema[0].upper()
            and columns==['site','parent_url','url','label','kind','matched_item','seen_at']
            and not db.execute("SELECT 1 FROM sqlite_master WHERE name='_mirror_links_snapshot' OR (type='trigger' AND tbl_name='mirror_links')").fetchone()
            and not db.execute("PRAGMA foreign_key_list(mirror_links)").fetchone()
            and not db.execute("SELECT 1 FROM mirror_links WHERE site IS NULL OR parent_url IS NULL OR url IS NULL LIMIT 1").fetchone()):
        indexes=[row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='mirror_links' AND sql IS NOT NULL")]
        with db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE _mirror_links_snapshot ("+schema[0].split('(',1)[1]+" WITHOUT ROWID")
            db.execute("INSERT INTO _mirror_links_snapshot SELECT * FROM mirror_links")
            db.execute("DROP TABLE mirror_links")
            db.execute("ALTER TABLE _mirror_links_snapshot RENAME TO mirror_links")
            for sql in indexes:db.execute(sql)
    # Reclaim the old table/index pages before hashing and uploading the copy.
    db.execute("VACUUM")


def _publish_snapshot(store, namespace, bucket, profile="hf"):
    from boto3.s3.transfer import TransferConfig
    client = s3_client(namespace, profile)
    config = TransferConfig(multipart_threshold=256 * 1024**2, multipart_chunksize=256 * 1024**2, max_concurrency=2)
    store.export()
    # Consistent SQLite backup, not a copy of a live DB/WAL. Pages builds derive
    # HTML and their single JSON export from this same source of truth.
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "index.sqlite3"
        with closing(sqlite3.connect(snapshot)) as backup:
            store.db.backup(backup)
            _compact_snapshot(backup)
        client.upload_file(str(snapshot), bucket, "index.sqlite3", Config=config)
        expected = digest(snapshot)
        if public_digest(namespace, bucket, "index.sqlite3") != (expected["sha256"], expected["size"]):
            raise ValueError("public SQLite snapshot read-back mismatch")
    print("catalog checkpoint published")


def doctor():
    failed = False
    for host in ["www.lathercraft.net", "web.archive.org", "vn-archive.s3.hf.co", "huggingface.co"]:
        try:
            addresses = sorted({r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
            print(host, "DNS OK", ", ".join(addresses))
        except OSError as e:
            failed = True
            print(host, "DNS FAILED", str(e))
    credentials = configparser.ConfigParser()
    credentials.read(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", str(Path.home() / ".aws/credentials")))
    print("AWS profile hf:", "configured" if credentials.has_section("hf") else "not found")
    for host, port in [("127.0.0.53", 53), ("1.1.1.1", 443)]:
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"{host}:{port} TCP OK")
        except OSError as e:
            print(f"{host}:{port} TCP FAILED {type(e).__name__}: {e}")
    # No credential values are printed.
    return int(failed)


def positive(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="archive")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor")
    p = subs.add_parser("bucket-init", help="create bucket and check authenticated access")
    p.add_argument("--namespace", default="vn-archive")
    p.add_argument("--bucket", default="mirrors")
    p.add_argument("--profile", default="hf")
    p = subs.add_parser("build-site", help="generate static Pages HTML and one manifest.json from SQLite")
    p.add_argument("--output", default="dist")
    p = subs.add_parser("alias-maker", help="merge manufacturer spelling variants in SQLite")
    p.add_argument("alias")
    p.add_argument("name")
    p = subs.add_parser("archive-batch", help="download, publish, verify and optionally evict local working copies")
    p.add_argument("--site", default="both", help="source key from mirror_sources.toml, official, or both")
    p.add_argument("--namespace", default="vn-archive")
    p.add_argument("--bucket", default="mirrors")
    p.add_argument("--profile", default="hf")
    p.add_argument("--limit", type=positive)
    p.add_argument("--checkpoint-every", type=positive, default=10)
    p.add_argument("--shards", type=positive, default=1)
    p.add_argument("--shard-index", type=int, default=0)
    p.add_argument("--delay", type=float, default=1)
    p.add_argument("--keep-local", action="store_true")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--reserve-gib", type=positive, default=16)
    for command in ["crawl", "download", "verify", "upload", "status"]:
        p = subs.add_parser(command)
        p.add_argument("--site", choices=["both", "lathercraft", "holyseal"] if command=="crawl" else None, default="both")
        if command in {"download", "upload"}:
            p.add_argument("--limit", type=positive)
        if command == "download":
            p.add_argument("--id", help="download one catalog source record")
        if command == "crawl":
            p.add_argument("--cached", action="store_true", help="resume using saved successful HTML")
            p.add_argument("--max-pages", type=positive)
            p.add_argument("--max-items", type=positive)
            p.add_argument("--lathercraft-url", default="http://www.lathercraft.net/items?page=1")
            p.add_argument("--holyseal-url", action="append", help="repeat to choose explicit archive seeds; default: trials, patches, movies")
        if command in {"crawl", "download"}:
            p.add_argument("--delay", type=float, default=1)
        if command == "upload":
            p.add_argument("--namespace", default="vn-archive")
            p.add_argument("--bucket", default="mirrors")
            p.add_argument("--profile", default="hf")
    args = parser.parse_args(argv)
    if hasattr(args,"site") and args.site not in {"both","official"}:
        from mirror_sources import definitions
        if args.site not in definitions():parser.error("unregistered source: "+args.site)
    if args.command == "archive-batch" and not 0 <= args.shard_index < args.shards:
        parser.error("--shard-index must be between 0 and --shards - 1")
    if args.command == "doctor":
        return doctor()
    if args.command == "bucket-init":
        init_bucket(args.namespace, args.bucket, args.profile)
        return 0
    store = Store(args.root)
    try:
        if args.command == "archive-batch":
            from bulk import archive_batch
            stats = archive_batch(store, Fetcher(delay=args.delay, timeout=25, retries=2, tls_pins=source_tls_pins()),
                args.namespace, args.bucket, args.profile, args.site, args.limit, args.keep_local,
                args.reserve_gib * 1024**3, args.checkpoint_every, args.retry_failed, args.shards, args.shard_index)
            return int(bool(stats["failed"]))
        if args.command == "build-site":
            from catalog import build_site
            build_site(store, Path(args.output))
        if args.command == "alias-maker":
            from catalog import alias_maker
            alias_maker(store, args.alias, args.name)
        if args.command == "crawl":
            fetcher = Fetcher(delay=args.delay)
            if args.cached:
                fetcher = CachedPageFetcher(store, fetcher)
            failed = 0
            holy_seeds = args.holyseal_url or [HOLY, HOLY.replace("flt=try", "flt=fix"), HOLY.replace("flt=try", "flt=demo")]
            for site, seed in [("lathercraft", args.lathercraft_url), *[("holyseal", u) for u in holy_seeds]]:
                if args.site in {"both", site}:
                    failed += crawl(store, fetcher, site, seed, args.max_pages, args.max_items)
            return int(bool(failed))
        if args.command == "download":
            return int(bool(download(store, Fetcher(delay=args.delay, tls_pins=source_tls_pins()), args.site, args.limit, args.id)))
        if args.command == "verify":
            return int(bool(verify(store, args.site)))
        if args.command == "upload":
            publish(store, args.namespace, args.bucket, args.profile, args.site, args.limit)
        if args.command == "status":
            for row in store.db.execute("SELECT site,status,count(*) FROM items WHERE (?='both' OR site=?) GROUP BY site,status", (args.site, args.site)):
                print(*row)
        return 0
    finally:
        store.export()
        store.db.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
