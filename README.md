# web-clipper

A Claude Code plugin that clips a web page into an Obsidian vault the way the
[Obsidian Web Clipper](https://github.com/obsidianmd/obsidian-clipper) browser extension does.
A clip is one Markdown note: the page's full body under the extension's frontmatter, with every
image, inline SVG and canvas graph saved next to it as a local attachment.

```
raw/What Are Tokens.md                                ← the note, frontmatter + body
raw/assets/what-are-tokens/what-are-tokens-01-…png    ← each image, linked as ![[…]]
raw/assets/what-are-tokens/assets.json                ← which URL became which file
```

## Install

In a shell, add the marketplace and install the plugin:

```bash
claude plugin marketplace add jasonsie/web-clipper
claude plugin install web-clipper@web-clipper-marketplace
```

The plugin drives the extension's own conversion code, so it needs three things on the machine:

| Need | Install |
|---|---|
| Node 21 or later | `nvm install 22` |
| The Obsidian Web Clipper CLI, built | `git clone https://github.com/obsidianmd/obsidian-clipper ~/Developer/forks/tools/obsidian_clipper && cd $_ && npm install && npm run build:cli` |
| Python Playwright with Chromium | `pip install playwright && playwright install chromium` |

[`SKILL.md`](skills/web-clipper/SKILL.md#prerequisites-one-time) covers the required CLI
version and how to point at other locations.

## Use

Ask Claude to clip a URL, or run the skill by name, `/web-clipper:web-clipper <url>`. Claude runs
the `web-clipper` command from inside your vault and reports the note, the saved images and any
warnings.

Some pages can't be fetched without a browser session: bot challenges, logins, paywalls. For
those, save the page from your own browser and pass the file back with `--html <file>`; the
skill walks you through it.

Settings such as another template or CLI path go in `~/.config/web-clipper/config.json`.
Before 2.1.0 that file was `~/.config/zkfy/web-clipper.json`; move it if you have one.

## Support

The plugin is listed in [ai-dotfiles](https://github.com/jasonsie/ai-dotfiles)' `config/plugins.json`
at an exact version, and installed through each CLI's own plugin manager: the Claude and Codex
marketplaces, a Pi package, and Antigravity's plugin install.

| | Claude Code | Codex | Pi | Antigravity |
|---|---|---|---|---|
| Skills | `web-clipper` | `web-clipper` | `web-clipper` | `web-clipper` |
| `web-clipper` command on PATH | Plugin `bin/` is on PATH; ai-dotfiles also links it into `~/.local/bin` | ai-dotfiles links `bin/web-clipper` into `~/.local/bin` | ai-dotfiles links `bin/web-clipper` into `~/.local/bin` | ai-dotfiles links `bin/web-clipper` into `~/.local/bin` |
| Hooks | none shipped | none shipped | none shipped | none shipped |
| MCP servers | none shipped | none shipped | none shipped | none shipped |
| Agents | none shipped | none shipped | none shipped | none shipped |

ai-dotfiles links the command from a checkout at the pinned tag, so the shell finds it too.

## Call it from another plugin

While the plugin is enabled, its `bin/` folder is on the PATH of Claude's Bash tool, so your
plugin's agents and skills run the bare command:

```bash
web-clipper "<url>" --out raw --json
```

Declare the dependency in your `plugin.json`, and allow this marketplace in your
`marketplace.json`, so Claude Code installs web-clipper with your plugin:

```json
"dependencies": [{ "name": "web-clipper", "marketplace": "web-clipper-marketplace", "version": "^2.1" }]
```

```json
"allowCrossMarketplaceDependenciesOn": ["web-clipper-marketplace"]
```

What stays stable within a major version:

- **Exit codes**: `0` ok, `1` prerequisites or usage, `2` fetch failed or page blocked (rerun with
  `--html`), `3` output exists (`--force` overwrites), `4` conversion failed, `5` note written
  but some images stayed remote.
- **`--json`**: one JSON line on stdout. A clip has `note`, `url`, `requested_url`, `via`,
  `template`, `title`, `assets`, `assets_dir`, `fetch` and `warnings`; `--localize` has `note`,
  `url`, `via`, `title`, `assets`, `assets_dir`, `changed` and `warnings`. `assets` counts `saved`,
  `failed`, `graphs` and `svg_blocks`.
- **`--version`**: prints `web-clipper <version>`.

Call the command, not a path inside the plugin: the installed copy moves with every version.

## Develop

```bash
python3 -m unittest discover -s tests -v   # the end-to-end tests skip without the CLI, Node or Playwright
claude plugin validate .
claude --plugin-dir .                      # a session that uses this checkout instead of the installed copy
```

[`AGENTS.md`](AGENTS.md) holds the rules for changes and the release steps.

## History

The clipper began in [zkfy](https://github.com/jasonsie/zkfy) as a shell script, was rewritten
in Python in the author's dotfiles (`jasonsie/ai-dotfiles#28`), and moved here in November 2026
with that history.

- **2.2.1**: native Antigravity manifest; per-CLI support table.

## License

MIT, see [LICENSE](LICENSE). The default template comes from Obsidian Web Clipper; see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
