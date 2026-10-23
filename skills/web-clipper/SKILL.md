---
name: web-clipper
description: Clip a web page into an Obsidian vault the way Obsidian Web Clipper does — the full body under the clip frontmatter (source/title/author/published/created/description/tags: row) with every image, inline SVG and canvas graph saved locally as wikilinked attachments. Use when asked to clip, save, archive or "crawl back" a URL, or to localize remote images in an existing clip. A page that cannot be fetched headlessly is saved from the user's browser and passed back with --html.
---

# Web Clipper

`$ARGUMENTS — <url> [--out <dir|file.md>] [--html <file>] [--keep-html] [--force]`
`$ARGUMENTS — --localize <existing-note.md>`

## What it produces

```
raw/<Page Title>.md                      ← frontmatter + full body, same shape as Obsidian Web Clipper
raw/assets/<slug>/<slug>-01-<name>.png   ← every image (largest srcset candidate), inline SVG, canvas graph
raw/assets/<slug>/assets.json            ← manifest: url → file, sha256, origin
raw/assets/<slug>/page.html              ← only with --keep-html
```

Frontmatter (template `templates/row.json`, the extension default with `tags: row`):

```yaml
---
source: "https://…"
title: "…"
author: "[[Name]]"
published: 2026-04-27
created: 2026-10-23
description: "…"
tags:
  - "row"
---
```

Images become `![[slug-01-name.png|alt]]`. The attachment folder comes from the vault's
`.obsidian/app.json` (`attachmentFolderPath`), so the links resolve wherever the note is later moved.
The filename is the page title with `# ^ [ ] | / : \` removed, like the extension on macOS.

## Run it

Always through Bash: the script writes the note itself (vault hooks may block the Write/Edit tools
under `raw/`). Run from inside the vault, or pass `--vault <root>`.

```bash
python3 ~/.claude/skills/web-clipper/scripts/clip.py <url>                     # → <vault>/raw/<Title>.md
python3 ~/.claude/skills/web-clipper/scripts/clip.py <url> --out raw/ai        # choose the folder
python3 ~/.claude/skills/web-clipper/scripts/clip.py <url> --html page.html    # page saved from a browser
python3 ~/.claude/skills/web-clipper/scripts/clip.py --localize "raw/cs/x.md"  # fetch an old clip's remote images
python3 ~/.claude/skills/web-clipper/scripts/clip.py <url> --json              # machine-readable summary
```

Codex and Pi find the same folder at `~/.agents/skills/web-clipper/`, Antigravity at
`~/.gemini/config/skills/web-clipper/`.

Exit codes: `0` ok · `1` prerequisites or usage · `2` fetch failed or page blocked (see below) ·
`3` output exists (use `--force`) · `4` conversion failed · `5` note written but some assets stayed remote.

After a run, read the summary on stderr and tell the user the note path, how many assets were saved,
and every warning verbatim. Do not post-edit the clip with Write/Edit; rerun with `--force` instead.

## Limitation, and the one recourse: `--html`

Headless fetching cannot get past a bot challenge ("Just a moment…", HTTP 403/429), a login wall or
paywall (member-only posts, subscriber newsletters, X articles), or an app that renders nothing
without a session. The script detects these (status code, challenge titles, under 200 characters of
visible text) and stops with **exit 2**. There is no scraping service behind it, by design.

Recourse:

1. Get the rendered page from the user's own browser, where they are logged in:
   - ask the user: Chrome/Edge `⌘S` → "Webpage, HTML Only", or DevTools → Elements → `<html>` →
     Copy → Copy outerHTML into a file; or
   - with Claude in Chrome: navigate to the page, run `document.documentElement.outerHTML` with the
     JavaScript tool, and write the result to a file outside `raw/` (the scratchpad is fine).
2. Rerun with the file. The URL is still required: it fills `source:` and resolves relative links.
   ```bash
   python3 ~/.claude/skills/web-clipper/scripts/clip.py <url> --html <file>
   ```

A false alarm (an image-only page with little text) can be pushed through with `--ignore-blocked`.
Other limits: content revealed only by interaction is not captured; a WebGL canvas may screenshot
blank; inlined SVG styles do not cover external `<use>` sprites or CSS animation; the image cache
recorded from the browser session is capped at 200 MB (the rest is downloaded).

## Prerequisites (one-time)

| Need | Check | Install |
|---|---|---|
| Node ≥ 21 | `node --version` | `nvm install 22` |
| Obsidian Web Clipper CLI | `ls ~/Developer/forks/tools/obsidian_clipper/dist/cli.cjs` | `git clone https://github.com/obsidianmd/obsidian-clipper ~/Developer/forks/tools/obsidian_clipper && cd $_ && npm install && npm run build:cli` |
| Python Playwright + Chromium | `python3 -c "import playwright"` | `pip install playwright && playwright install chromium` |

The CLI must be upstream `obsidianmd/obsidian-clipper` at or after commit `5315b30` (after tag 1.7.1);
1.6.2 and 1.7.1 return empty clips. Override locations with `--cli`/`--node`/`--template`,
`OBSIDIAN_CLIPPER_CLI`, or `~/.config/zkfy/web-clipper.json`:

```json
{ "cli": "/path/to/obsidian_clipper/dist/cli.cjs", "node": "/path/to/node", "template": "/path/to/my-clipper.json" }
```

## How it works

```
URL ──▶ [1] fetch ──▶ HTML snapshot ──▶ [2] obsidian-clipper CLI ──▶ Markdown ──▶ [3] assets ──▶ raw/<Title>.md
          │                                   (defuddle + template)                  │
          ├ Playwright: headless Chromium, scrolls for lazy images, records image     ├ bytes from the browser
          │   responses, swaps <canvas>/<svg> ≥48px (outside nav/sidebars) for        │   session, data: URIs, or a
          │   sized <img> placeholders (https://clip-assets.invalid/graph-NN.*)       │   download with UA + Referer
          └ --html FILE|-: the page as saved from a browser                           ├ inline <svg> blocks → .svg files
                                                                                     └ links → ![[file|alt]] wikilinks
```

Stage 2 is the extension's own code, so code fences (with language), pipe tables, footnotes,
callouts, math and the og:image hero rule behave exactly as in a manual clip.

## Template

`templates/row.json` is the extension default with `tags: row` and `source` first. For byte-identical
output with the user's own template: Web Clipper → Settings → the template → Export, then pass
`--template <file>` (or set it in the config). Template rules in the CLI:

- every property needs a `type` (`text`, `multitext`, `date`, `datetime`, `number`, `checkbox`);
- `noteNameFormat`, `noteContentFormat`, `properties` are required; daily-note exports lack them;
- avoid prompt variables (`{{"…"}}`), `{{highlights}}`, `{{selection}}`, the `remove_html`,
  `html_to_json` and `truncate` filters — they render empty or are unavailable outside the browser.

## Verification

```bash
cd ~/Developer/mine/ai/ai_dotfiles && python3 -m unittest discover -s scripts/tests -p 'test_web_clipper.py' -v
python3 ~/.claude/skills/web-clipper/scripts/clip.py https://www.aihero.dev/what-are-tokens --out /tmp/clips --json
```
