"""Run: python3 -m unittest discover -s tests -v

Unit tests run offline. The end-to-end tests need the built Obsidian Web Clipper CLI
(OBSIDIAN_CLIPPER_CLI or ~/Developer/forks/tools/obsidian_clipper/dist/cli.cjs) and Node 21+;
the Playwright test additionally needs `python3 -c "import playwright"` to succeed.
"""
import base64
import contextlib
import http.server
import io
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/web-clipper/scripts/clip.py"
BIN = ROOT / "bin" / "web-clipper"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
SPEC = importlib.util.spec_from_file_location("clip", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
clip = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = clip  # @dataclass looks its module up here
SPEC.loader.exec_module(clip)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.html"
CLI = Path(os.environ.get("OBSIDIAN_CLIPPER_CLI") or clip.DEFAULT_CLI)

# The --json summary keys other plugins read (zkfy reads note, assets, warnings).
CLIP_KEYS = {"note", "url", "requested_url", "via", "template", "title", "assets", "assets_dir", "fetch",
             "warnings"}
LOCALIZE_KEYS = {"note", "url", "via", "title", "assets", "assets_dir", "changed", "warnings"}
ASSET_KEYS = {"saved", "failed", "graphs", "svg_blocks"}
# zkfy's clip-to-zk reads these from --adopt / --release / --unadopt.
ADOPT_KEYS = {"note", "adopted_from", "assets_dir", "assets", "dry_run", "warnings"}
RELEASE_KEYS = {"note", "adopted_to", "released", "warnings"}
UNADOPT_KEYS = {"note", "removed", "warnings"}


def png_bytes(width=64, height=64, rgb=(200, 50, 50)):
    """Smallest valid RGB PNG, generated so the fixtures carry no binaries."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def node_ok():
    try:
        clip.node_binary(None)
        return True
    except clip.ClipError:
        return False


def playwright_ok():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - keep test output clean
        del format, args


class ServedFixture:
    """Serve a copy of the fixture (plus generated PNGs) on a loopback port."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="web-clipper-www-")
        root = Path(self.tmp.name)
        shutil.copy(FIXTURE, root / "sample.html")
        (root / "img1.png").write_bytes(png_bytes(120, 80, (200, 50, 50)))
        (root / "img2.png").write_bytes(png_bytes(100, 60, (50, 50, 200)))
        handler = lambda *a, **k: QuietHandler(*a, directory=str(root), **k)  # noqa: E731
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/sample.html"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()


def make_vault(root: Path, attachment_setting="raw/assets"):
    root = root.resolve()  # macOS temp dirs live under /private/var; the script resolves paths too
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "app.json").write_text(json.dumps({"attachmentFolderPath": attachment_setting}))
    (root / "raw").mkdir()
    return root


def run_clip(*args, cwd=None):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True,
                          cwd=cwd, timeout=240)


class NamesTest(unittest.TestCase):
    def test_sanitize_filename_matches_obsidian_clips(self):
        self.assertEqual(clip.sanitize_filename("Claude Code 團隊揭秘: 9 種 Skills 類型 + 9 個寫作技巧"),
                         "Claude Code 團隊揭秘 9 種 Skills 類型 + 9 個寫作技巧")
        self.assertEqual(clip.sanitize_filename('a/b\\c:d*e?f"g<h>i|j#k^l[m]n'), 'abcd*e?f"g<h>ijklmn')
        self.assertEqual(clip.sanitize_filename("  What Are Tokens?  "), "What Are Tokens?")
        self.assertEqual(clip.sanitize_filename(".hidden"), "_hidden")
        self.assertEqual(clip.sanitize_filename("   "), "Untitled")
        self.assertEqual(clip.sanitize_filename("", fallback="x"), "x")
        self.assertLessEqual(len(clip.sanitize_filename("字" * 300).encode("utf-8")), 240)

    def test_slugify(self):
        self.assertEqual(clip.slugify("What Are Tokens?"), "what-are-tokens")
        self.assertEqual(clip.slugify("Claude Code 團隊揭秘: 9 種"), "claude-code-團隊揭秘-9-種")
        self.assertEqual(clip.slugify("x" * 80), "x" * 40)
        self.assertEqual(clip.slugify("???"), "clip")

    def test_url_helpers(self):
        self.assertEqual(clip.url_stem("https://cdn.x/img/photo%20one.jpg?w=10"), "photo-one")
        self.assertEqual(clip.url_stem("https://clip-assets.invalid/graph-01.svg"), "graph")
        self.assertEqual(clip.url_stem("data:image/png;base64,AAAA"), "inline")
        self.assertEqual(clip.url_stem("https://x/_next/image"), "image")
        self.assertEqual(clip.url_slug("https://www.aihero.dev/what-are-tokens"), "www.aihero.dev what-are-tokens")


class FrontmatterTest(unittest.TestCase):
    MD = ('---\nsource: "https://x/y"\ntitle: "He said \\"hi\\": now"\nauthor: "[[A]],[[B]]"\npublished:\n'
          'created: 2026-10-23\ndescription: "d"\ntags:\n  - "row"\n---\nBody line\n')

    def test_parse(self):
        data, body = clip.parse_frontmatter(self.MD)
        self.assertEqual(data["title"], 'He said "hi": now')
        self.assertEqual(data["author"], "[[A]],[[B]]")
        self.assertEqual(data["published"], "")
        self.assertEqual(data["created"], "2026-10-23")
        self.assertEqual(data["tags"], ["row"])
        self.assertEqual(body, "Body line\n")

    def test_no_frontmatter(self):
        self.assertEqual(clip.parse_frontmatter("plain"), ({}, "plain"))


class MarkdownScanTest(unittest.TestCase):
    def test_image_refs(self):
        md = ('![a](https://x/a.png) text ![b c](https://x/b.png "title") '
              '![d](https://x/File_\\(1\\).png) ![e](<https://x/a b.png>) '
              '[![f](https://x/f.png)](https://x/page) ![g\\]h](https://x/g.png) ![no]() [not image](https://x)')
        refs = clip.find_image_refs(md)
        self.assertEqual([r.url for r in refs], ["https://x/a.png", "https://x/b.png", "https://x/File_(1).png",
                                                 "https://x/a b.png", "https://x/f.png", "https://x/g.png"])
        self.assertEqual(refs[1].title, "title")
        self.assertEqual(refs[5].alt, "g]h")
        self.assertEqual(md[refs[0].start:refs[0].end], "![a](https://x/a.png)")
        self.assertEqual(md[refs[1].start:refs[1].end], '![b c](https://x/b.png "title")')

    def test_svg_blocks_and_fences(self):
        md = "x <svg><svg></svg><rect/></svg> y\n```\n<svg></svg>\n```\n<SVG viewBox='0 0 1 1'></SVG>"
        blocks = clip.find_svg_blocks(md)
        self.assertEqual(md[blocks[0][0]:blocks[0][1]], "<svg><svg></svg><rect/></svg>")
        self.assertEqual(len(blocks), 3)
        fences = clip.fenced_ranges(md)
        self.assertEqual(len(fences), 1)
        self.assertTrue(fences[0][0] <= blocks[1][0] < fences[0][1])

    def test_wikilink(self):
        self.assertEqual(clip.wikilink("a.png", "alt | with pipe]]"), "![[a.png|alt / with pipe] ]]]")
        self.assertEqual(clip.wikilink("a.png", "  "), "![[a.png]]")

    def test_sniff_ext(self):
        self.assertEqual(clip.sniff_ext(png_bytes(), "", "https://x/a"), "png")
        self.assertEqual(clip.sniff_ext(b"\xff\xd8\xff\xe0", "", "x"), "jpg")
        self.assertEqual(clip.sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "", "x"), "webp")
        self.assertEqual(clip.sniff_ext(b'<?xml version="1.0"?><svg xmlns="x"></svg>', "", "x"), "svg")
        self.assertEqual(clip.sniff_ext(b"\xef\xbb\xbf<svg></svg>", "text/plain", "x"), "svg")
        self.assertEqual(clip.sniff_ext(b"????", "image/avif", "x"), "avif")
        self.assertEqual(clip.sniff_ext(b"????", "", "https://x/p.JPEG?x=1"), "jpg")
        self.assertIsNone(clip.sniff_ext(b"<html>", "text/html", "https://x/p.png"))
        self.assertIsNone(clip.sniff_ext(b"????", "", "https://x/_next/image?url=1"))


class AttachmentBaseTest(unittest.TestCase):
    def test_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(Path(tmp))
            note_dir = vault / "raw" / "ai"
            self.assertEqual(clip.attachment_base(vault, note_dir, None), (vault / "raw/assets").resolve())
            (vault / ".obsidian/app.json").write_text(json.dumps({"attachmentFolderPath": "./media"}))
            self.assertEqual(clip.attachment_base(vault, note_dir, None), (note_dir / "media").resolve())
            (vault / ".obsidian/app.json").write_text(json.dumps({"attachmentFolderPath": "/"}))
            self.assertEqual(clip.attachment_base(vault, note_dir, None), vault)
            (vault / ".obsidian/app.json").write_text("{}")
            self.assertEqual(clip.attachment_base(vault, note_dir, None), vault)
            self.assertEqual(clip.attachment_base(vault, note_dir, str(vault / "z")), (vault / "z").resolve())
            self.assertEqual(clip.attachment_base(None, note_dir, None), (note_dir / "assets").resolve())
            self.assertEqual(clip.find_vault(note_dir), vault)
            self.assertIsNone(clip.find_vault(Path(tempfile.gettempdir()) / "nope-not-a-vault"))


class LocalizeTest(unittest.TestCase):
    def test_localize_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(Path(tmp))
            png = png_bytes()
            svg = b"<svg xmlns='http://www.w3.org/2000/svg' width='100' height='100'><rect width='10' height='10'/></svg>"
            fetched = clip.Fetched(html="", url="https://site.test/post/", via="test",
                                   recorded={"https://cdn.test/a.png": ("image/png", png),
                                             "https://site.test/post/rel/b.jpg": ("image/jpeg", b"\xff\xd8\xff\xdb")},
                                   graphs={"graph-01": ("svg", svg)})
            md = ("---\ntitle: \"T\"\n---\n"
                  "![Alpha](https://cdn.test/a.png)\n\n"
                  "![Beta](rel/b.jpg \"Beta title\")\n\n"
                  "![Chart](https://clip-assets.invalid/graph-01.svg)\n\n"
                  "![Data](data:image/png;base64," + base64.b64encode(png).decode() + ")\n\n"
                  "<svg xmlns='http://www.w3.org/2000/svg'><circle r='1'/></svg>\n\n"
                  "<table><tr><td><img src=\"https://cdn.test/a.png\" alt=\"dup\"></td></tr></table>\n\n"
                  "```\n![in code](https://cdn.test/a.png)\n```\n\n"
                  "![Gone](http://127.0.0.1:9/missing.png)\n")
            store = clip.AssetStore(vault / "raw/assets/t", "t", vault)
            warnings = []
            out, stats = clip.localize_markdown(md, fetched, fetched.url, store, "UA", 5, warnings)
            store.write_manifest()
            self.assertEqual(stats["saved"], 6)        # references localized (the <img> dup reuses a file)
            self.assertEqual(len(store.entries), 5)    # unique files written
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["graphs"], 1)
            self.assertEqual(stats["svg_blocks"], 1)
            self.assertIn("![[t-01-a.png|Alpha]]", out)
            self.assertIn("![[t-02-b.jpg|Beta]]", out)
            self.assertIn("![[t-03-graph.svg|Chart]]", out)
            self.assertIn("![[t-04-inline.png|Data]]", out)
            self.assertIn("![[t-05-graph.svg]]", out)
            self.assertIn('<img src="raw/assets/t/t-01-a.png" alt="dup">', out)
            self.assertIn("```\n![in code](https://cdn.test/a.png)\n```", out)
            self.assertIn("![Gone](http://127.0.0.1:9/missing.png)", out)
            self.assertTrue(any("missing.png" in w for w in warnings))
            files = sorted(p.name for p in (vault / "raw/assets/t").iterdir())
            self.assertEqual(files, ["assets.json", "t-01-a.png", "t-02-b.jpg", "t-03-graph.svg",
                                     "t-04-inline.png", "t-05-graph.svg"])
            manifest = json.loads((vault / "raw/assets/t/assets.json").read_text())
            self.assertEqual({e["origin"] for e in manifest["assets"]},
                             {"recorded", "graph", "inline", "inline-svg"})
            # Re-running is idempotent: the manifest is reused, nothing is duplicated.
            store2 = clip.AssetStore(vault / "raw/assets/t", "t", vault)
            self.assertEqual(store2.add("https://cdn.test/a.png", png, "png", "image/png", "recorded", "a"),
                             "t-01-a.png")
            self.assertEqual(len(list((vault / "raw/assets/t").iterdir())), 6)


class AdoptTest(unittest.TestCase):
    """--adopt copies a clip to its vault note, --release drops the raw copy, --unadopt drops the adopted one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = make_vault(Path(self.tmp.name))
        (self.vault / "111.cs").mkdir()
        self.old_dir = self.vault / "raw/assets/my-page"
        self.old_dir.mkdir(parents=True)
        self.files = {"my-page-01-a.png": png_bytes(), "my-page-02-b.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>"}
        entries = []
        for name, data in self.files.items():
            (self.old_dir / name).write_bytes(data)
            entries.append({"file": name, "url": f"https://site.test/{name}", "sha256": clip.hashlib.sha256(data).hexdigest()})
        (self.old_dir / "assets.json").write_text(json.dumps({"slug": "my-page", "assets": entries}))
        self.note = self.vault / "raw/My Page.md"
        self.note.write_text('---\nsource: "https://site.test/"\ntitle: "My Page"\n---\n'
                             "Intro.\n![[my-page-01-a.png|Alpha]]\n"
                             "```md\n![[my-page-01-a.png]]\n```\n"
                             '<table><tr><td><img src="raw/assets/my-page/my-page-02-b.svg" alt="b"></td></tr></table>\n'
                             "![[my-page-02-b.svg]]\n", encoding="utf-8")
        self.dest = self.vault / "111.cs/CS-My-Page.md"
        self.new_dir = self.vault / "raw/assets/CS-My-Page"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = clip.main([*args, "--vault", str(self.vault), "--json"])
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else {}), err.getvalue()

    def adopt(self, *extra):
        return self.run_main("--adopt", str(self.note), "--to", str(self.dest), *extra)

    def test_dry_run_writes_nothing(self):
        code, summary, err = self.adopt("--dry-run")
        self.assertEqual(code, clip.EXIT_OK, err)
        self.assertEqual(set(summary), ADOPT_KEYS)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["assets"], {"renamed": 2, "embeds": 3})
        self.assertFalse(self.dest.exists() or self.new_dir.exists())

    def test_adopt_copies_renames_and_rewrites_outside_fences(self):
        code, summary, err = self.adopt()
        self.assertEqual(code, clip.EXIT_OK, err)
        self.assertEqual((summary["note"], summary["assets_dir"]), (str(self.dest), str(self.new_dir)))
        text = self.dest.read_text(encoding="utf-8")
        self.assertIn("![[CS-My-Page-01-a.png|Alpha]]", text)
        self.assertIn("```md\n![[my-page-01-a.png]]\n```", text)          # fenced: left as written
        self.assertIn('<img src="raw/assets/CS-My-Page/CS-My-Page-02-b.svg" alt="b">', text)
        self.assertIn("![[CS-My-Page-02-b.svg]]", text)
        self.assertEqual(sorted(p.name for p in self.new_dir.iterdir()),
                         ["CS-My-Page-01-a.png", "CS-My-Page-02-b.svg", "assets.json"])
        manifest = json.loads((self.new_dir / "assets.json").read_text())
        self.assertEqual(manifest["slug"], "CS-My-Page")
        self.assertEqual([e["file"] for e in manifest["assets"]], ["CS-My-Page-01-a.png", "CS-My-Page-02-b.svg"])
        self.assertTrue(self.note.exists() and (self.old_dir / "my-page-01-a.png").exists())  # raw kept

    def test_refuses_collisions_and_double_adoption(self):
        self.dest.write_text("x")
        self.assertEqual(self.adopt()[0], clip.EXIT_EXISTS)
        self.dest.unlink()
        self.assertEqual(self.adopt()[0], clip.EXIT_OK)
        self.assertEqual(self.run_main("--adopt", str(self.note), "--to", str(self.vault / "111.cs/CS-Other.md"))[0],
                         clip.EXIT_EXISTS)                                  # already adopted
        self.assertEqual(self.run_main("--adopt", str(self.note), "--to", str(self.vault / "nope/CS-X.md"))[0],
                         clip.EXIT_USAGE)                                   # never creates folders

    def test_checksum_mismatch_leaves_nothing_behind(self):
        (self.old_dir / "my-page-02-b.svg").write_bytes(b"changed")
        code, _, err = self.adopt()
        self.assertEqual(code, clip.EXIT_CONVERT, err)
        self.assertFalse(self.dest.exists() or self.new_dir.exists())
        self.assertFalse((self.vault / "raw/assets/.adoptions.json").exists())

    def test_release_deletes_only_the_raw_copy(self):
        self.assertEqual(self.run_main("--release", str(self.note))[0], clip.EXIT_USAGE)  # nothing adopted yet
        self.adopt()
        code, summary, err = self.run_main("--release", str(self.note))
        self.assertEqual(code, clip.EXIT_OK, err)
        self.assertEqual(set(summary), RELEASE_KEYS)
        self.assertFalse(self.note.exists() or self.old_dir.exists())
        self.assertTrue(self.dest.exists() and (self.new_dir / "CS-My-Page-01-a.png").exists())
        self.assertEqual(self.run_main("--release", str(self.note))[0], clip.EXIT_USAGE)

    def test_release_refuses_when_the_adopted_note_is_gone(self):
        self.adopt()
        self.dest.unlink()
        self.assertEqual(self.run_main("--release", str(self.note))[0], clip.EXIT_USAGE)
        self.assertTrue(self.note.exists())

    def test_unadopt_removes_the_copy_and_allows_a_retry(self):
        self.adopt()
        code, summary, err = self.run_main("--unadopt", str(self.note))
        self.assertEqual(code, clip.EXIT_OK, err)
        self.assertEqual(set(summary), UNADOPT_KEYS)
        self.assertFalse(self.dest.exists() or self.new_dir.exists())
        self.assertTrue(self.note.exists() and (self.old_dir / "assets.json").exists())
        self.assertEqual(self.adopt()[0], clip.EXIT_OK)

    def test_localize_reuses_the_adopted_folder(self):
        self.adopt()
        data = base64.b64encode(png_bytes()).decode()
        with self.dest.open("a", encoding="utf-8") as f:
            f.write(f"![Dot](data:image/png;base64,{data})\n")
        code, _, err = self.run_main("--localize", str(self.dest))
        self.assertEqual(code, clip.EXIT_OK, err)
        self.assertIn("![[CS-My-Page-03-inline.png|Dot]]", self.dest.read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in (self.vault / "raw/assets").iterdir()),
                         [".adoptions.json", "CS-My-Page", "my-page"])  # no new slug folder


class ClipperErrorsTest(unittest.TestCase):
    def fake_cli(self, tmp, stdout="", stderr="", code=0):
        script = Path(tmp) / "fake.py"
        script.write_text(f"import sys\nsys.stdin.read()\nsys.stdout.write({stdout!r})\n"
                          f"sys.stderr.write({stderr!r})\nsys.exit({code})\n")
        return script

    def test_detects_cli_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "t.json"
            template.write_text("{}")
            ok = self.fake_cli(tmp, stdout="---\ntitle: \"x\"\n---\nbody")
            md, stderr = clip.run_clipper("<html>", "https://x", template, ok, sys.executable)
            self.assertTrue(md.startswith("---"))
            self.assertEqual(stderr, "")
            cases = [
                dict(stdout="x", stderr="Template compilation errors: Line 1"),
                dict(stdout="x", stderr="Invalid filter: truncate"),
                dict(stdout="Partial conversion completed with errors. Original HTML:"),
                dict(stdout="", stderr="", code=1),
                dict(stdout="   "),
            ]
            for case in cases:
                with self.assertRaises(clip.ClipError) as ctx:
                    clip.run_clipper("<html>", "https://x", template, self.fake_cli(tmp, **case), sys.executable)
                self.assertEqual(ctx.exception.code, clip.EXIT_CONVERT, case)


class ContractTest(unittest.TestCase):
    """What callers rely on: the command, its exit codes, the --json summary, the config path."""

    def test_exit_codes(self):
        self.assertEqual((clip.EXIT_OK, clip.EXIT_USAGE, clip.EXIT_FETCH, clip.EXIT_EXISTS, clip.EXIT_CONVERT,
                          clip.EXIT_ASSETS), (0, 1, 2, 3, 4, 5))

    def test_config_path(self):
        self.assertEqual(clip.CONFIG_PATH, Path.home() / ".config" / "web-clipper" / "config.json")

    def test_command_runs_through_a_link_from_any_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "bin" / "web-clipper"
            link.parent.mkdir()
            link.symlink_to(BIN)
            version = subprocess.run([str(link), "--version"], capture_output=True, text=True, cwd=tmp,
                                     timeout=30)
            self.assertEqual((version.returncode, version.stdout), (0, f"web-clipper {clip.VERSION}\n"),
                             version.stderr)
            missing = subprocess.run([str(link), "--localize", "nope.md"], capture_output=True, text=True,
                                     cwd=tmp, timeout=30)
            self.assertEqual(missing.returncode, clip.EXIT_USAGE, missing.stderr)
            self.assertIn("note not found", missing.stderr)

    def test_localize_json_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(Path(tmp))
            note = vault / "raw" / "Clip.md"
            data = base64.b64encode(png_bytes()).decode()
            note.write_text(f'---\nsource: "https://site.test/"\ntitle: "Clip"\n---\n\n'
                            f"![Dot](data:image/png;base64,{data})\n")
            proc = subprocess.run([str(BIN), "--localize", str(note), "--json"], capture_output=True, text=True,
                                  cwd=str(vault), timeout=60)
            self.assertEqual(proc.returncode, clip.EXIT_OK, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(set(summary), LOCALIZE_KEYS)
            self.assertEqual(set(summary["assets"]), ASSET_KEYS)
            self.assertEqual(summary["assets"]["saved"], 1)
            self.assertIn("![[clip-01-inline.png|Dot]]", note.read_text())


class PackagingTest(unittest.TestCase):
    """The names and the version zkfy's dependency entry resolves against."""

    def test_names_and_version_agree(self):
        plugin = json.loads(PLUGIN.read_text())
        market = json.loads(MARKETPLACE.read_text())
        self.assertEqual((plugin["name"], plugin["version"]), ("web-clipper", clip.VERSION))
        self.assertEqual(market["name"], "web-clipper-marketplace")
        entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
        self.assertEqual(entry["source"], "./")
        self.assertNotIn("version", entry)  # plugin.json holds the one version; validate warns on two

    def test_command_is_executable(self):
        self.assertTrue(os.access(BIN, os.X_OK), BIN)


@unittest.skipUnless(CLI.is_file() and node_ok(), "obsidian-clipper CLI not built or Node < 21")
class EndToEndHtmlTest(unittest.TestCase):
    def test_html_input_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(Path(tmp) / "vault")
            url = "http://127.0.0.1:9/sample.html"  # port 9: connection refused, remote images stay remote
            proc = run_clip(url, "--html", str(FIXTURE), "--json", "--cli", str(CLI), cwd=str(vault))
            self.assertEqual(proc.returncode, clip.EXIT_ASSETS, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(set(summary), CLIP_KEYS)
            self.assertEqual(set(summary["assets"]), ASSET_KEYS)
            note = Path(summary["note"])
            self.assertEqual(note, vault / "raw" / "Sample Clip Page.md")
            text = note.read_text()
            fm, body = clip.parse_frontmatter(text)
            self.assertEqual(list(fm), ["source", "title", "author", "published", "created", "description", "tags"])
            self.assertEqual(fm["title"], "Sample Clip Page")
            self.assertEqual(fm["author"], "[[Ada Lovelace]]")
            self.assertEqual(fm["published"], "2026-01-15")
            self.assertEqual(fm["tags"], ["row"])
            self.assertIn("```python\ndef add(a, b):", body)
            self.assertIn("| alpha | 1 |", body)
            self.assertIn("[^1]", body)
            self.assertNotIn("Footer links", body)
            self.assertIn("![[sample-clip-page-", body)            # data: image and inline svg localized
            self.assertIn("![First image](http://127.0.0.1:9/img1.png)", body)  # unreachable: left remote
            self.assertEqual(summary["assets"]["svg_blocks"], 1)
            assets = sorted(p.name for p in (vault / "raw/assets/sample-clip-page").iterdir())
            self.assertIn("assets.json", assets)
            self.assertTrue(any(a.endswith(".svg") for a in assets))
            self.assertTrue(any(a.endswith("inline.png") for a in assets))
            # Second run refuses to overwrite.
            again = run_clip(url, "--html", str(FIXTURE), "--cli", str(CLI), cwd=str(vault))
            self.assertEqual(again.returncode, clip.EXIT_EXISTS, again.stderr)
            # A page that cannot be fetched (connection refused here) exits 2 and points at --html.
            blocked = run_clip("http://127.0.0.1:9/", "--cli", str(CLI), "--timeout", "5", cwd=str(vault))
            self.assertEqual(blocked.returncode, clip.EXIT_FETCH, blocked.stderr)
            self.assertIn("--html", blocked.stderr)


@unittest.skipUnless(CLI.is_file() and node_ok() and playwright_ok(), "needs built CLI, Node 21+ and Playwright")
class EndToEndPlaywrightTest(unittest.TestCase):
    def test_full_pipeline_with_graphs(self):
        with ServedFixture() as www, tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(Path(tmp) / "vault")
            proc = run_clip(www.url, "--json", "--cli", str(CLI), "--keep-html",
                            cwd=str(vault / "raw"))
            self.assertEqual(proc.returncode, clip.EXIT_OK, proc.stderr)
            summary = json.loads(proc.stdout)
            note = Path(summary["note"])
            self.assertEqual(note, vault / "raw" / "Sample Clip Page.md")
            body = clip.parse_frontmatter(note.read_text())[1]
            asset_dir = vault / "raw/assets/sample-clip-page"
            names = sorted(p.name for p in asset_dir.iterdir())
            self.assertIn("page.html", names)
            self.assertIn("assets.json", names)
            self.assertEqual(summary["assets"]["failed"], 0, summary)
            self.assertEqual(summary["assets"]["graphs"], 2, summary)   # svg + canvas placeholders
            self.assertGreaterEqual(summary["assets"]["saved"], 5, summary)
            self.assertIn("![[sample-clip-page-01-img1.png|First image]]", body)
            self.assertIn("Figure 1 caption", body)
            self.assertRegex(body, r"!\[\[sample-clip-page-\d\d-graph\.svg\|Bar chart\]\]")
            self.assertRegex(body, r"!\[\[sample-clip-page-\d\d-graph\.png\|Canvas chart\]\]")
            svg_file = next(p for p in asset_dir.iterdir() if p.suffix == ".svg")
            self.assertIn("fill:rgb(70, 130, 180)", svg_file.read_text())   # steelblue from CSS, inlined
            png_file = next(p for p in asset_dir.iterdir() if p.name.endswith("graph.png"))
            self.assertTrue(png_file.read_bytes().startswith(b"\x89PNG"))
            manifest = json.loads((asset_dir / "assets.json").read_text())
            self.assertIn("recorded", {e["origin"] for e in manifest["assets"]})

    def test_localize_existing_note(self):
        with ServedFixture() as www, tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(Path(tmp) / "vault")
            note = vault / "raw" / "Old clip.md"
            note.write_text(f'---\nsource: "{www.url}"\ntitle: "Old clip"\n---\n\n![First](img1.png)\n\n'
                            f'![Second]({www.url.rsplit("/", 1)[0]}/img2.png)\n')
            proc = run_clip("--localize", str(note), "--json", cwd=str(vault))
            self.assertEqual(proc.returncode, clip.EXIT_OK, proc.stderr)
            text = note.read_text()
            self.assertIn("![[old-clip-01-img1.png|First]]", text)
            self.assertIn("![[old-clip-02-img2.png|Second]]", text)
            self.assertTrue((vault / "raw/assets/old-clip/old-clip-01-img1.png").is_file())


if __name__ == "__main__":
    unittest.main()
