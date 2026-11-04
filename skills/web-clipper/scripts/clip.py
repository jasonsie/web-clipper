#!/usr/bin/env python3
"""web-clipper: capture a web page into an Obsidian vault the way the
Obsidian Web Clipper extension does, with images and graphs stored locally.

Pipeline
    1. fetch    Playwright headless Chromium | --html FILE|- (page saved from a browser) -> HTML snapshot
    2. convert  Obsidian Web Clipper CLI (bundles defuddle) + template     -> Markdown + frontmatter
    3. assets   images / inline SVG / canvas graphs -> <attachments>/<slug>/ ; links -> ![[wikilinks]]

Usage
    web-clipper <url> [--out DIR|FILE.md] [--html FILE|-] [--ignore-blocked]
                      [--template JSON] [--cli cli.cjs] [--node PATH] [--vault DIR]
                      [--assets-dir DIR] [--no-assets] [--keep-html] [--force]
                      [--timeout S] [--wait-for MS] [--user-agent UA] [--min-graph-px N] [--json]
    web-clipper --localize NOTE.md [--vault DIR] [--assets-dir DIR] [--json]
    web-clipper --adopt RAW.md --to DEST.md [--stem STEM] [--dry-run] [--vault DIR] [--json]
    web-clipper --release RAW.md [--vault DIR] [--json]     # after a good adoption: drop the raw copy
    web-clipper --unadopt RAW.md [--vault DIR] [--json]     # after a bad one: drop the adopted copy
    (web-clipper is bin/web-clipper, which runs this file; `python3 clip.py ...` is the same.)

Limitation and recourse
    A bot challenge, login wall, paywall, or an app that renders nothing without a session
    cannot be fetched headlessly. The script stops with exit 2; open the page in your own
    browser, save the rendered page (Save Page As > "Webpage, HTML Only", or copy
    document.documentElement.outerHTML to a file), and rerun with --html <file>.

Exit codes
    0 ok · 1 usage/preflight · 2 fetch failed or blocked (use --html) · 3 output exists
    4 convert failed · 5 note written but some assets failed (left as remote links)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VERSION = "2.2.0"  # = .claude-plugin/plugin.json; a test keeps them equal
SKILL_DIR = Path(__file__).resolve().parent.parent
INPAGE_JS = SKILL_DIR / "scripts" / "inpage.js"
DEFAULT_TEMPLATE = SKILL_DIR / "templates" / "row.json"
DEFAULT_CLI = Path.home() / "Developer" / "forks" / "tools" / "obsidian_clipper" / "dist" / "cli.cjs"
CONFIG_PATH = Path.home() / ".config" / "web-clipper" / "config.json"
SYNTHETIC_HOST = "https://clip-assets.invalid/"
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
CLI_ERROR_PATTERNS = ("Template compilation errors", "Invalid filter", "No template matched", "Error in ")
PARTIAL_MARKER = "Partial conversion completed with errors"
BLOCKED_TITLE_RE = re.compile(
    r"just a moment|attention required|access denied|are you a robot|verify you are human|captcha|"
    r"403 forbidden|too many requests", re.I)
IMAGE_EXTS = {"png", "jpg", "gif", "webp", "avif", "svg", "bmp", "ico", "tif", "heic"}
RECORD_CAP_BYTES = 200 * 1024 * 1024
MIN_NODE_MAJOR = 21
MIN_TEXT_CHARS = 200

EXIT_OK, EXIT_USAGE, EXIT_FETCH, EXIT_EXISTS, EXIT_CONVERT, EXIT_ASSETS = 0, 1, 2, 3, 4, 5

HTML_RECOURSE = ("Headless fetching cannot get past a bot challenge, login wall, paywall or a page that renders "
                 "nothing without a session. Open the page in your own browser, save the rendered page "
                 "(Save Page As > \"Webpage, HTML Only\", or copy document.documentElement.outerHTML to a file), "
                 "then rerun with --html <file>.")

AUTOSCROLL_JS = """async () => {
  const step = Math.max(300, Math.round(window.innerHeight * 0.8));
  let y = 0;
  for (let i = 0; i < 80; i++) {
    y += step;
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, 120));
    if (y >= document.documentElement.scrollHeight) break;
  }
  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 300));
}"""

WAIT_IMAGES_JS = """() => Promise.race([
  Promise.all(Array.from(document.images).filter(i => !i.complete).map(i => new Promise(r => {
    i.addEventListener('load', r, { once: true });
    i.addEventListener('error', r, { once: true });
  }))),
  new Promise(r => setTimeout(r, 8000)),
])"""

UNTAG_JS = """(ids) => ids.forEach(id => {
  const el = document.querySelector('[data-zkclip="' + id + '"]');
  if (el) el.removeAttribute('data-zkclip');
})"""


class ClipError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- config / vault

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log(f"warning: ignoring unreadable config {CONFIG_PATH}: {exc}")
        return {}


def find_vault(start: Path) -> Optional[Path]:
    for candidate in [start, *start.parents]:
        if (candidate / ".obsidian").is_dir():
            return candidate
    return None


def attachment_base(vault: Optional[Path], note_dir: Path, override: Optional[str]) -> Path:
    """Resolve Obsidian's attachmentFolderPath: '/' = vault root, './x' = beside the note, else vault-relative."""
    if override:
        return Path(override).expanduser().resolve()
    if vault is None:
        return (note_dir / "assets").resolve()
    setting = ""
    try:
        app = json.loads((vault / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        setting = str(app.get("attachmentFolderPath", "") or "")
    except (OSError, ValueError):
        pass
    if not setting or setting == "/":
        return vault
    if setting == "./":
        return note_dir.resolve()
    if setting.startswith("./"):
        return (note_dir / setting[2:]).resolve()
    return (vault / setting).resolve()


# ----------------------------------------------------------------------------- stage 1: fetch

@dataclass
class Fetched:
    html: str
    url: str
    via: str
    recorded: dict = field(default_factory=dict)   # url -> (content_type, bytes)
    graphs: dict = field(default_factory=dict)     # graph id -> (ext, bytes)
    stats: dict = field(default_factory=dict)
    blocked: Optional[str] = None


def read_html_input(spec: str, url: str) -> Fetched:
    if spec == "-":
        html = sys.stdin.read()
    else:
        html = Path(spec).expanduser().read_text(encoding="utf-8", errors="replace")
    if not html.strip():
        raise ClipError("empty HTML input", EXIT_FETCH)
    return Fetched(html=html, url=url, via="html", stats={"bytes": len(html)})


def fetch_playwright(url: str, args: argparse.Namespace, ua: str, warnings: list) -> Fetched:
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ClipError("python playwright is not installed: pip install playwright && playwright install chromium",
                        EXIT_FETCH) from exc
    js = INPAGE_JS.read_text(encoding="utf-8")
    responses: list = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2,
                                          user_agent=ua, locale="en-US")
            page = context.new_page()
            page.on("response", lambda resp: responses.append(resp))
            try:
                main = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            except PWTimeout as exc:
                raise ClipError(f"timed out loading {url} after {args.timeout}s", EXIT_FETCH) from exc
            except Exception as exc:  # net::ERR_* and friends
                raise ClipError(f"navigation failed: {exc}", EXIT_FETCH) from exc
            status = main.status if main else 0
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass
            if args.wait_for:
                page.wait_for_timeout(args.wait_for)
            page.evaluate(AUTOSCROLL_JS)
            page.evaluate(WAIT_IMAGES_JS)

            title = page.title() or ""
            text_len = page.evaluate("() => ((document.body && document.body.innerText) || '').length")
            blocked = None
            if status in (401, 403, 407, 429, 503):
                blocked = f"HTTP {status}"
            elif BLOCKED_TITLE_RE.search(title):
                blocked = f"title {title[:60]!r}"
            elif text_len < MIN_TEXT_CHARS:
                blocked = f"only {text_len} characters of visible text"

            fixed = page.evaluate(js, {"phase": "images"}) or {}
            items = page.evaluate(js, {"phase": "prepare", "minPx": args.min_graph_px, "host": SYNTHETIC_HOST}) or []
            graphs: dict = {}
            alts: dict = {}
            failed: list = []
            for item in items:
                gid = item["id"]
                alts[gid] = item.get("alt") or ""
                if item["kind"] == "canvas":
                    try:
                        png = page.locator(f'[data-zkclip="{gid}"]').first.screenshot(type="png", timeout=10000)
                        graphs[gid] = ("png", png)
                    except Exception as exc:
                        warnings.append(f"canvas {gid} screenshot failed: {exc}")
                        failed.append(gid)
                elif item.get("data"):
                    graphs[gid] = ("svg", item["data"].encode("utf-8"))
                else:
                    failed.append(gid)
            if failed:
                page.evaluate(UNTAG_JS, failed)
            replaced = page.evaluate(js, {"phase": "replace", "host": SYNTHETIC_HOST, "alts": alts}) or {}

            html = page.content()
            final_url = page.url
            recorded: dict = {}
            total = 0
            for resp in responses:
                try:
                    if resp.status != 200:
                        continue
                    ctype = resp.headers.get("content-type", "")
                    if not (ctype.startswith("image/") or resp.request.resource_type == "image"):
                        continue
                    body = resp.body()
                except Exception:
                    continue
                total += len(body)
                if total > RECORD_CAP_BYTES:
                    warnings.append("recorded image cache cap reached; remaining images will be downloaded")
                    break
                recorded[resp.url] = (ctype, body)
            stats = {"status": status, "images_fixed": fixed.get("fixed", 0), "graphs_tagged": len(items),
                     "graphs_replaced": replaced.get("replaced", 0), "responses_recorded": len(recorded),
                     "text_chars": text_len}
            return Fetched(html=html, url=final_url, via="playwright", recorded=recorded, graphs=graphs,
                           stats=stats, blocked=blocked)
        finally:
            browser.close()


def fetch(url: str, args: argparse.Namespace, ua: str, warnings: list) -> Fetched:
    if args.html:
        return read_html_input(args.html, url)
    fetched = fetch_playwright(url, args, ua, warnings)
    if fetched.blocked:
        if args.ignore_blocked:
            warnings.append(f"page looks blocked ({fetched.blocked}); continuing because --ignore-blocked was set")
            return fetched
        raise ClipError(f"page looks blocked ({fetched.blocked}). {HTML_RECOURSE}", EXIT_FETCH)
    return fetched


# ----------------------------------------------------------------------------- stage 2: convert

def node_binary(explicit: Optional[str]) -> str:
    node = explicit or shutil.which("node")
    if not node:
        raise ClipError(f"node not found on PATH (Node {MIN_NODE_MAJOR}+ required)", EXIT_USAGE)
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipError(f"cannot run {node}: {exc}", EXIT_USAGE) from exc
    match = re.match(r"v?(\d+)", version)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        raise ClipError(f"Node {version or '?'} is too old; the clipper CLI needs Node {MIN_NODE_MAJOR}+ "
                        "(it reads navigator.platform)", EXIT_USAGE)
    return node


def resolve_cli(explicit: Optional[str]) -> Path:
    cli = Path(explicit).expanduser() if explicit else DEFAULT_CLI
    if not cli.is_file():
        raise ClipError(
            f"obsidian-clipper CLI not found at {cli}. Build it with: cd {cli.parent.parent} && npm ci && "
            f"npm run build:cli  (or point --cli / OBSIDIAN_CLIPPER_CLI / {CONFIG_PATH} at dist/cli.cjs)",
            EXIT_USAGE)
    return cli


def run_clipper(html: str, url: str, template: Path, cli: Path, node: str, timeout: int = 180) -> tuple[str, str]:
    cmd = [node, str(cli), url, "--html", "-", "-t", str(template)]
    try:
        proc = subprocess.run(cmd, input=html, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ClipError(f"clipper CLI timed out after {timeout}s", EXIT_CONVERT) from exc
    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        raise ClipError(f"clipper CLI exited {proc.returncode}: {stderr[-800:]}", EXIT_CONVERT)
    hits = [p.strip() for p in CLI_ERROR_PATTERNS if p in stderr]
    if hits:
        raise ClipError(f"clipper CLI reported {hits}: {stderr[-800:]}", EXIT_CONVERT)
    md = proc.stdout
    if PARTIAL_MARKER in md:
        raise ClipError("defuddle could not convert the page to Markdown (partial conversion)", EXIT_CONVERT)
    if not md.strip():
        raise ClipError("clipper CLI produced empty output", EXIT_CONVERT)
    return md, stderr


# ----------------------------------------------------------------------------- markdown helpers

def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"')
    return value


def parse_frontmatter(md: str) -> tuple[dict, str]:
    """Parse the flat YAML the clipper writes (text, lists, dates). Returns (data, body)."""
    if not md.startswith("---\n"):
        return {}, md
    lines = md.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, md
    data: dict = {}
    key: Optional[str] = None
    for line in lines[1:end]:
        if line.startswith("  - ") and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_unquote(line[4:].strip()))
            continue
        match = re.match(r'^("?)([^"\n]+?)\1:\s?(.*)$', line)
        if match:
            key = match.group(2)
            data[key] = _unquote(match.group(3).strip())
    return data, "\n".join(lines[end + 1:])


def sanitize_filename(name: str, fallback: str = "Untitled") -> str:
    """Web Clipper's macOS file-name rule: strip `# ^ [ ] | / : \\` and control characters, keep `? * " < >`."""
    cleaned = re.sub(r'[\\/:|#^\[\]\x00-\x1f\x7f]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".").strip()
    if cleaned.startswith("."):
        cleaned = "_" + cleaned[1:]
    cleaned = cleaned.encode("utf-8")[:240].decode("utf-8", "ignore").strip()
    return cleaned or fallback


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^\w]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "clip"


def url_slug(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parts.path).strip("/")
    return f"{parts.netloc} {path}".replace("/", " ").strip() or url


def url_stem(url: str) -> str:
    if url.startswith(SYNTHETIC_HOST):
        return "graph"
    if url.startswith("data:"):
        return "inline"
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).rstrip("/")
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[^\w.-]+", "-", stem).strip("-.")
    return stem[:40] or "image"


FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.M)


def fenced_ranges(md: str) -> list:
    ranges = []
    open_pos = None
    open_char = ""
    open_len = 0
    for match in FENCE_RE.finditer(md):
        marker = match.group(1)
        if open_pos is None:
            open_pos, open_char, open_len = match.start(), marker[0], len(marker)
        elif marker[0] == open_char and len(marker) >= open_len:
            ranges.append((open_pos, match.end()))
            open_pos = None
    if open_pos is not None:
        ranges.append((open_pos, len(md)))
    return ranges


@dataclass
class ImageRef:
    start: int
    end: int
    alt: str
    url: str
    title: Optional[str]


IMG_START_RE = re.compile(r"!\[((?:[^\[\]\\]|\\.)*)\]\(")
HTML_IMG_RE = re.compile(r"""<img\b[^>]*?\ssrc\s*=\s*(["'])(.*?)\1""", re.I | re.S)
SVG_TOKEN_RE = re.compile(r"<svg\b|</svg\s*>", re.I)


def find_image_refs(md: str) -> list:
    """Markdown image references, including `<dest>` form, escaped parentheses and titles."""
    refs = []
    size = len(md)
    for match in IMG_START_RE.finditer(md):
        i = match.end()
        while i < size and md[i] in " \t":
            i += 1
        if i < size and md[i] == "<":
            close = md.find(">", i + 1)
            if close == -1:
                continue
            dest = md[i + 1:close]
            i = close + 1
        else:
            chars = []
            depth = 0
            while i < size:
                ch = md[i]
                if ch == "\\" and i + 1 < size:
                    chars.append(md[i + 1])
                    i += 2
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch in " \t\n":
                    break
                chars.append(ch)
                i += 1
            dest = "".join(chars)
        while i < size and md[i] in " \t":
            i += 1
        title = None
        if i < size and md[i] in "\"'":
            quote = md[i]
            close = md.find(quote, i + 1)
            if close == -1:
                continue
            title = md[i + 1:close]
            i = close + 1
            while i < size and md[i] in " \t":
                i += 1
        if i < size and md[i] == ")" and dest:
            alt = re.sub(r"\\(.)", r"\1", match.group(1))
            refs.append(ImageRef(match.start(), i + 1, alt, dest, title))
    return refs


def find_svg_blocks(md: str) -> list:
    blocks = []
    depth = 0
    start = None
    for match in SVG_TOKEN_RE.finditer(md):
        if match.group(0).lower().startswith("<svg"):
            if depth == 0:
                start = match.start()
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append((start, match.end()))
                start = None
    return blocks


def wikilink(name: str, alt: str) -> str:
    alt = re.sub(r"\s+", " ", alt or "").replace("|", "/").replace("]]", "] ]").strip()
    return f"![[{name}|{alt}]]" if alt else f"![[{name}]]"


# ----------------------------------------------------------------------------- stage 3: assets

def sniff_ext(data: bytes, content_type: str, url: str) -> Optional[str]:
    head = data[:64]
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF8"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    text = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if (text.startswith(b"<?xml") or text.startswith(b"<svg") or text.startswith(b"<!")) and b"<svg" in text:
        return "svg"
    ctype = (content_type or "").split(";")[0].strip().lower()
    table = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
             "image/webp": "webp", "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp",
             "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/tiff": "tif",
             "image/heic": "heic"}
    if ctype in table:
        return table[ctype]
    if ctype and not ctype.startswith("image/") and ctype != "application/octet-stream":
        return None
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    return ext if ext in IMAGE_EXTS else None


def download(url: str, referer: str, ua: str, timeout: int, retries: int = 2) -> tuple[bytes, str]:
    last: Optional[Exception] = None
    for _ in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
    raise LookupError(f"download failed: {last}")


def resolve_bytes(url: str, fetched: Fetched, referer: str, ua: str, timeout: int) -> tuple[bytes, str, str]:
    if url.startswith(SYNTHETIC_HOST):
        gid = url[len(SYNTHETIC_HOST):].rsplit(".", 1)[0]
        if gid in fetched.graphs:
            ext, data = fetched.graphs[gid]
            return data, ("image/svg+xml" if ext == "svg" else "image/png"), "graph"
        raise LookupError("captured graph bytes missing")
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        ctype = header[5:].split(";")[0] or "application/octet-stream"
        data = base64.b64decode(payload) if ";base64" in header else urllib.parse.unquote_to_bytes(payload)
        return data, ctype, "inline"
    rec = fetched.recorded.get(url) or fetched.recorded.get(url.split("#")[0])
    if rec:
        return rec[1], rec[0], "recorded"
    data, ctype = download(url, referer, ua, timeout)
    return data, ctype, "download"


@dataclass
class AssetStore:
    directory: Path
    slug: str
    vault: Optional[Path]
    entries: list = field(default_factory=list)
    by_key: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        manifest = self.directory / "assets.json"
        if manifest.is_file():
            try:
                for entry in json.loads(manifest.read_text(encoding="utf-8")).get("assets", []):
                    if (self.directory / entry["file"]).is_file():
                        self.entries.append(entry)
                        self.by_key[entry["url"]] = entry["file"]
            except (OSError, ValueError, KeyError):
                pass

    def add(self, key: str, data: bytes, ext: str, content_type: str, origin: str, stem: str, alt: str = "") -> str:
        if key in self.by_key:
            return self.by_key[key]
        index = len(self.entries) + 1
        stem = re.sub(r"[^\w.-]+", "-", stem).strip("-.")[:40] or "image"
        name = f"{self.slug}-{index:02d}-{stem}.{ext}"
        self.directory.mkdir(parents=True, exist_ok=True)
        if (self.directory / name).exists():
            name = f"{self.slug}-{index:02d}-{stem}-{hashlib.sha1(key.encode()).hexdigest()[:6]}.{ext}"
        (self.directory / name).write_bytes(data)
        self.entries.append({"file": name, "url": key, "origin": origin, "content_type": content_type,
                             "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "alt": alt})
        self.by_key[key] = name
        return name

    def vault_relative(self, name: str) -> str:
        path = self.directory / name
        if self.vault is not None:
            try:
                return path.relative_to(self.vault).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    def write_manifest(self) -> None:
        if not self.entries:
            return
        payload = {"slug": self.slug, "assets": self.entries}
        (self.directory / "assets.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")


def localize_markdown(md: str, fetched: Fetched, base_url: str, store: AssetStore, ua: str, timeout: int,
                      warnings: list) -> tuple[str, dict]:
    """Replace every image/graph reference in `md` with a local wikilink; returns (markdown, stats)."""
    fences = fenced_ranges(md)
    stats = {"saved": 0, "failed": 0, "graphs": 0, "svg_blocks": 0}
    dl_timeout = max(10, min(timeout, 30))

    candidates = []  # (start, end, kind, payload)
    for start, end in find_svg_blocks(md):
        candidates.append((start, end, "svg", md[start:end]))
    for ref in find_image_refs(md):
        candidates.append((ref.start, ref.end, "md", ref))
    for match in HTML_IMG_RE.finditer(md):
        candidates.append((match.start(2), match.end(2), "html", match.group(2)))
    candidates.sort(key=lambda c: c[0])

    def store_url(raw_url: str, alt: str) -> Optional[str]:
        url = raw_url.strip()
        if not url.startswith(("data:", SYNTHETIC_HOST)):
            url = urllib.parse.urljoin(base_url, url)
        try:
            data, ctype, origin = resolve_bytes(url, fetched, base_url, ua, dl_timeout)
        except Exception as exc:  # noqa: BLE001 - any failure leaves the remote link in place
            warnings.append(f"asset failed, left remote: {url[:140]} ({exc})")
            stats["failed"] += 1
            return None
        ext = sniff_ext(data, ctype, url)
        if not ext:
            warnings.append(f"not an image, left remote: {url[:140]} ({ctype or 'unknown type'})")
            stats["failed"] += 1
            return None
        name = store.add(url, data, ext, ctype, origin, url_stem(url), alt)
        stats["saved"] += 1
        if origin == "graph":
            stats["graphs"] += 1
        return name

    edits = []
    last_end = -1
    for start, end, kind, payload in candidates:
        if start < last_end or any(a <= start < b for a, b in fences):
            continue
        if kind == "svg":
            svg_bytes = payload.encode("utf-8")
            key = "inline-svg:" + hashlib.sha1(svg_bytes).hexdigest()
            name = store.add(key, svg_bytes, "svg", "image/svg+xml", "inline-svg", "graph")
            stats["saved"] += 1
            stats["svg_blocks"] += 1
            edits.append((start, end, f"![[{name}]]"))
        elif kind == "md":
            name = store_url(payload.url, payload.alt)
            if name:
                edits.append((start, end, wikilink(name, payload.alt)))
        elif store.vault is not None and not urllib.parse.urlparse(payload).scheme and \
                (store.vault / payload).is_file():
            pass  # an <img> already pointing at a vault file (an earlier localize or an adoption)
        else:
            name = store_url(payload, "")
            if name:
                edits.append((start, end, store.vault_relative(name)))
        last_end = end

    out = md
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + replacement + out[end:]
    return out, stats


# ----------------------------------------------------------------------------- commands

def report(summary: dict, as_json: bool, warnings: list) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(summary["note"])
    assets = summary.get("assets") or {}
    log(f"note:   {summary['note']}")
    if summary.get("via"):
        log(f"via:    {summary['via']}  url: {summary.get('url', '')}")
    if "renamed" in assets:
        log(f"assets: {assets['renamed']} renamed, {assets['embeds']} links rewritten -> {summary.get('assets_dir')}")
    elif assets:
        log(f"assets: {assets.get('saved', 0)} saved, {assets.get('failed', 0)} left remote, "
            f"{assets.get('graphs', 0)} graphs captured, {assets.get('svg_blocks', 0)} inline svg "
            f"-> {summary.get('assets_dir', '')}")
    for warning in warnings:
        log(f"warning: {warning}")


def cmd_clip(args: argparse.Namespace, cfg: dict, warnings: list) -> int:
    if not args.url:
        raise ClipError("a URL is required (or use --localize NOTE.md)", EXIT_USAGE)
    url = args.url
    cwd = Path.cwd()
    vault = Path(args.vault).expanduser().resolve() if args.vault else find_vault(cwd)

    if args.out:
        out_path = Path(args.out).expanduser()
        if not out_path.is_absolute():
            out_path = (cwd / out_path).resolve()
        if vault is None:
            vault = find_vault(out_path.parent if out_path.suffix.lower() == ".md" else out_path)
    elif vault is not None and (vault / "raw").is_dir():
        out_path = vault / "raw"
    elif vault is not None:
        out_path = vault
    else:
        raise ClipError("no Obsidian vault found above the current directory; pass --vault or --out", EXIT_USAGE)

    node = node_binary(args.node or cfg.get("node"))
    cli = resolve_cli(args.cli or os.environ.get("OBSIDIAN_CLIPPER_CLI") or cfg.get("cli"))
    template = Path(args.template or cfg.get("template") or DEFAULT_TEMPLATE).expanduser()
    if not template.is_file():
        raise ClipError(f"template not found: {template}", EXIT_USAGE)
    ua = args.user_agent or cfg.get("user_agent") or DEFAULT_UA

    fetched = fetch(url, args, ua, warnings)
    log(f"fetched via {fetched.via}: {fetched.url} ({fetched.stats})")
    md, cli_stderr = run_clipper(fetched.html, fetched.url, template, cli, node)
    if cli_stderr:
        warnings.append(f"clipper stderr: {cli_stderr.splitlines()[0][:300]}")

    frontmatter, _ = parse_frontmatter(md)
    title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else ""
    title = (title or "").strip()
    fallback_name = sanitize_filename(url_slug(url))
    note_name = sanitize_filename(title, fallback=fallback_name)
    if not title:
        warnings.append(f"page has no title; using {note_name!r}")
    note_path = out_path if out_path.suffix.lower() == ".md" else out_path / f"{note_name}.md"
    if note_path.exists() and not args.force:
        raise ClipError(f"output exists: {note_path} (use --force to overwrite)", EXIT_EXISTS)

    slug = slugify(title or url_slug(url))
    store: Optional[AssetStore] = None
    stats: dict = {}
    if not args.no_assets or args.keep_html:
        base = attachment_base(vault, note_path.parent, args.assets_dir)
        asset_dir = base / slug
        if args.force and asset_dir.exists():
            if (asset_dir / "assets.json").exists():
                shutil.rmtree(asset_dir)
            else:
                warnings.append(f"asset folder exists without a manifest; files may be overwritten: {asset_dir}")
        store = AssetStore(asset_dir, slug, vault)
    if args.keep_html and store is not None:
        store.directory.mkdir(parents=True, exist_ok=True)
        (store.directory / "page.html").write_text(fetched.html, encoding="utf-8")
    if not args.no_assets and store is not None:
        md, stats = localize_markdown(md, fetched, fetched.url, store, ua, args.timeout, warnings)
        store.write_manifest()

    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(md, encoding="utf-8")
    summary = {"note": str(note_path), "url": fetched.url, "requested_url": url, "via": fetched.via,
               "template": str(template), "title": title, "assets": stats,
               "assets_dir": str(store.directory) if store is not None else None,
               "fetch": fetched.stats, "warnings": warnings}
    report(summary, args.json, warnings)
    return EXIT_ASSETS if stats.get("failed") else EXIT_OK


def cmd_localize(args: argparse.Namespace, cfg: dict, warnings: list) -> int:
    note_path = Path(args.localize).expanduser().resolve()
    if not note_path.is_file():
        raise ClipError(f"note not found: {note_path}", EXIT_USAGE)
    md = note_path.read_text(encoding="utf-8")
    frontmatter, _ = parse_frontmatter(md)
    source = frontmatter.get("source") if isinstance(frontmatter.get("source"), str) else ""
    title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else ""
    if not source:
        warnings.append("no `source:` in frontmatter; relative image links cannot be resolved")
    base_url = source or "https://localhost/"
    vault = Path(args.vault).expanduser().resolve() if args.vault else find_vault(note_path.parent)
    base = attachment_base(vault, note_path.parent, args.assets_dir)
    # An adopted note keeps its images in <base>/<note stem>/; a raw clip uses the title slug.
    slug = note_path.stem if (base / note_path.stem / "assets.json").is_file() else slugify(title or note_path.stem)
    store = AssetStore(base / slug, slug, vault)
    ua = args.user_agent or cfg.get("user_agent") or DEFAULT_UA
    fetched = Fetched(html="", url=base_url, via="localize")
    new_md, stats = localize_markdown(md, fetched, base_url, store, ua, args.timeout, warnings)
    store.write_manifest()
    if new_md != md:
        note_path.write_text(new_md, encoding="utf-8")
    summary = {"note": str(note_path), "url": source, "via": "localize", "title": title, "assets": stats,
               "assets_dir": str(store.directory), "changed": new_md != md, "warnings": warnings}
    report(summary, args.json, warnings)
    return EXIT_ASSETS if stats.get("failed") else EXIT_OK


# ----------------------------------------------------------------------------- adopt / release / unadopt
#
# Adoption moves a clip out of raw/ in two phases, so one complete copy exists at every step:
#   --adopt    copy the note to DEST and its images to <attachments>/<stem>/, renamed <stem>-NN-...
#   --release  delete the raw clip and its old image folder (only for a recorded adoption)
#   --unadopt  delete the adopted copy instead, keeping the raw clip
# Each adoption is recorded in <attachments of the raw note>/.adoptions.json, keyed by the raw note.

EMBED_RE = re.compile(r"!\[\[([^\]|\n]+?)(\|[^\]\n]*)?\]\]")
LEDGER = ".adoptions.json"


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _key(path: Path, vault: Optional[Path]) -> str:
    if vault is not None:
        try:
            return path.relative_to(vault).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _inside(path: Path, vault: Optional[Path]) -> bool:
    if vault is None:
        return True
    try:
        path.relative_to(vault)
        return True
    except ValueError:
        return False


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ClipError(f"unreadable {path}: {exc}", EXIT_USAGE) from exc


def _write_json(path: Path, data: dict) -> None:
    if data:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def _source(args: argparse.Namespace, note_arg: str) -> tuple[Path, Optional[Path], Path, Path, str]:
    """The raw note, the vault, its attachment base, its image folder and slug."""
    note = _resolve(note_arg)
    if not note.is_file():
        raise ClipError(f"note not found: {note}", EXIT_USAGE)
    vault = _resolve(args.vault) if args.vault else find_vault(note.parent)
    frontmatter, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
    title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else ""
    slug = slugify(title or note.stem)
    base = attachment_base(vault, note.parent, args.assets_dir)
    return note, vault, base, base / slug, slug


def rewrite_targets(md: str, names: dict, paths: dict) -> tuple[str, int]:
    """Point image embeds (names) and <img src> paths (paths) at their new files, outside code fences."""
    fences = fenced_ranges(md)
    edits = []
    for match in EMBED_RE.finditer(md):
        target = match.group(1).strip()
        if target in names and not any(a <= match.start() < b for a, b in fences):
            edits.append((match.start(1), match.end(1), names[target]))
    for match in HTML_IMG_RE.finditer(md):
        if match.group(2) in paths and not any(a <= match.start() < b for a, b in fences):
            edits.append((match.start(2), match.end(2), paths[match.group(2)]))
    for start, end, replacement in sorted(edits, reverse=True):
        md = md[:start] + replacement + md[end:]
    return md, len(edits)


def cmd_adopt(args: argparse.Namespace, cfg: dict, warnings: list) -> int:
    note, vault, base, old_dir, slug = _source(args, args.adopt)
    if not args.to:
        raise ClipError("--adopt needs --to DEST.md", EXIT_USAGE)
    dest = _resolve(args.to)
    stem = args.stem or dest.stem
    if dest.suffix.lower() != ".md" or dest == note:
        raise ClipError(f"--to must be a new .md path: {dest}", EXIT_USAGE)
    if not dest.parent.is_dir():
        raise ClipError(f"destination folder does not exist: {dest.parent}", EXIT_USAGE)
    if not _inside(dest, vault):
        raise ClipError(f"destination is outside the vault: {dest}", EXIT_USAGE)
    if dest.exists():
        raise ClipError(f"output exists: {dest}", EXIT_EXISTS)
    ledger_path = base / LEDGER
    ledger = _read_json(ledger_path)
    key = _key(note, vault)
    if key in ledger:
        raise ClipError(f"already adopted to {ledger[key]['to']}; --release or --unadopt it first", EXIT_EXISTS)

    manifest = _read_json(old_dir / "assets.json")
    entries = manifest.get("assets", []) if manifest else []
    new_dir = attachment_base(vault, dest.parent, args.assets_dir) / stem
    if entries and new_dir.exists():
        raise ClipError(f"asset folder exists: {new_dir}", EXIT_EXISTS)
    names, paths, new_entries = {}, {}, []
    for entry in entries:
        old = entry["file"]
        new = stem + old[len(slug):] if old.startswith(slug + "-") else f"{stem}-{old}"
        data = (old_dir / old).read_bytes() if (old_dir / old).is_file() else None
        if data is None or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise ClipError(f"{old_dir / old} is missing or does not match its sha256 in assets.json", EXIT_CONVERT)
        names[old] = new
        paths[_key(old_dir / old, vault)] = _key(new_dir / new, vault)
        new_entries.append({**entry, "file": new})
    md, embeds = rewrite_targets(note.read_text(encoding="utf-8"), names, paths)
    summary = {"note": str(dest), "adopted_from": str(note), "assets_dir": str(new_dir) if entries else None,
               "assets": {"renamed": len(names), "embeds": embeds}, "dry_run": bool(args.dry_run),
               "warnings": warnings}
    if args.dry_run:
        report(summary, args.json, warnings)
        return EXIT_OK

    try:
        if entries:
            new_dir.mkdir(parents=True)
            for old, new in names.items():
                shutil.copy2(old_dir / old, new_dir / new)
            for entry in new_entries:
                if hashlib.sha256((new_dir / entry["file"]).read_bytes()).hexdigest() != entry.get("sha256"):
                    raise ClipError(f"copy {entry['file']} does not match its sha256", EXIT_CONVERT)
            _write_json(new_dir / "assets.json", {"slug": stem, "assets": new_entries})
        dest.write_text(md, encoding="utf-8")
        ledger[key] = {"to": _key(dest, vault), "assets_from": _key(old_dir, vault) if entries else None,
                       "assets_to": _key(new_dir, vault) if entries else None, "files": names}
        _write_json(ledger_path, ledger)
    except BaseException:
        dest.unlink(missing_ok=True)
        if entries and new_dir.exists():
            shutil.rmtree(new_dir)
        raise
    report(summary, args.json, warnings)
    return EXIT_OK


def _vault_path(value: Optional[str], vault: Optional[Path]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() or vault is None else vault / path


def _remove_folder(folder: Optional[Path], files, warnings: list) -> list:
    removed = []
    if folder is None or not folder.is_dir():
        return removed
    for name in [*files, "assets.json", "page.html"]:
        path = folder / name
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed.append(str(path))
    try:
        folder.rmdir()
    except OSError:
        warnings.append(f"kept {folder}: it holds files this adoption did not write")
    return removed


def _adoption(args: argparse.Namespace, note_arg: str):
    note, vault, base, _, _ = _source(args, note_arg)
    ledger = _read_json(base / LEDGER)
    entry = ledger.get(_key(note, vault))
    if not entry:
        raise ClipError(f"no recorded adoption for {note}", EXIT_USAGE)
    return note, vault, base, ledger, entry


def cmd_release(args: argparse.Namespace, cfg: dict, warnings: list) -> int:
    note, vault, base, ledger, entry = _adoption(args, args.release)
    dest = _vault_path(entry["to"], vault)
    if dest is None or not dest.is_file():
        raise ClipError(f"the adopted note {entry['to']} is gone; --unadopt instead of releasing the only copy",
                        EXIT_USAGE)
    released = _remove_folder(_vault_path(entry.get("assets_from"), vault), entry.get("files", {}).keys(), warnings)
    note.unlink()
    released.insert(0, str(note))
    del ledger[_key(note, vault)]
    _write_json(base / LEDGER, ledger)
    report({"note": str(note), "adopted_to": str(dest), "released": released, "warnings": warnings},
           args.json, warnings)
    return EXIT_OK


def cmd_unadopt(args: argparse.Namespace, cfg: dict, warnings: list) -> int:
    note, vault, base, ledger, entry = _adoption(args, args.unadopt)
    removed = _remove_folder(_vault_path(entry.get("assets_to"), vault), entry.get("files", {}).values(), warnings)
    dest = _vault_path(entry["to"], vault)
    if dest is not None and dest.is_file():
        dest.unlink()
        removed.insert(0, str(dest))
    del ledger[_key(note, vault)]
    _write_json(base / LEDGER, ledger)
    report({"note": str(note), "removed": removed, "warnings": warnings}, args.json, warnings)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web-clipper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="page to clip")
    parser.add_argument("--out", help="output directory or .md path (default: <vault>/raw)")
    parser.add_argument("--vault", help="vault root (default: nearest ancestor with .obsidian/)")
    parser.add_argument("--ignore-blocked", action="store_true",
                        help="continue even when the page looks blocked (challenge page, HTTP 403, almost no text)")
    parser.add_argument("--html", metavar="FILE", help="use this HTML instead of fetching ('-' = stdin)")
    parser.add_argument("--template", help="Web Clipper template JSON (default: templates/row.json)")
    parser.add_argument("--cli", help="path to obsidian-clipper dist/cli.cjs")
    parser.add_argument("--node", help="node binary (Node 21+)")
    parser.add_argument("--assets-dir", help="attachment base folder (default: Obsidian attachmentFolderPath)")
    parser.add_argument("--no-assets", action="store_true", help="keep remote image links")
    parser.add_argument("--keep-html", action="store_true", help="save the fetched HTML next to the assets")
    parser.add_argument("--force", action="store_true", help="overwrite an existing note and its asset folder")
    parser.add_argument("--localize", metavar="NOTE", help="only localize images of an existing note")
    parser.add_argument("--adopt", metavar="NOTE", help="copy a raw clip to --to, its images renamed after the note")
    parser.add_argument("--to", metavar="DEST.md", help="destination note for --adopt (its folder must exist)")
    parser.add_argument("--stem", help="image name prefix for --adopt (default: the destination's stem)")
    parser.add_argument("--dry-run", action="store_true", help="with --adopt: report the plan, write nothing")
    parser.add_argument("--release", metavar="NOTE", help="delete an adopted raw clip and its old image folder")
    parser.add_argument("--unadopt", metavar="NOTE", help="delete an adoption's copy, keeping the raw clip")
    parser.add_argument("--timeout", type=int, default=60, help="navigation timeout in seconds (default 60)")
    parser.add_argument("--wait-for", type=int, default=0, metavar="MS", help="extra wait after load")
    parser.add_argument("--user-agent", help="override the browser/download User-Agent")
    parser.add_argument("--min-graph-px", type=int, default=48, help="min rendered size for svg/canvas capture")
    parser.add_argument("--json", action="store_true", help="print a JSON summary on stdout")
    parser.add_argument("--version", action="version", version=f"web-clipper {VERSION}")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    warnings: list = []
    try:
        if args.adopt:
            return cmd_adopt(args, cfg, warnings)
        if args.release:
            return cmd_release(args, cfg, warnings)
        if args.unadopt:
            return cmd_unadopt(args, cfg, warnings)
        if args.localize:
            return cmd_localize(args, cfg, warnings)
        return cmd_clip(args, cfg, warnings)
    except ClipError as exc:
        log(f"error: {exc}")
        if exc.code == EXIT_FETCH and "--html" not in str(exc):
            log(f"hint: {HTML_RECOURSE}")
        for warning in warnings:
            log(f"warning: {warning}")
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
