# web-clipper — maintainer notes

A Claude Code plugin: one skill, and the `web-clipper` command other plugins call. zkfy
depends on it (`web-clipper@web-clipper-marketplace`, `^2.1`). `CLAUDE.md` links to this file.

## Map

| Path | What |
|---|---|
| `.claude-plugin/plugin.json` | The plugin and its one `version` |
| `.claude-plugin/marketplace.json` | `web-clipper-marketplace`; its entry carries no `version` (validate warns on two) |
| `bin/web-clipper` | The command; Claude Code puts `bin/` on the Bash PATH while the plugin is enabled. Runs `clip.py` with the same arguments |
| `skills/web-clipper/SKILL.md` | How an agent clips: usage, exit codes, the `--html` recourse, prerequisites |
| `skills/web-clipper/scripts/clip.py` | The clipper (fetch, convert, assets): `VERSION`, exit codes, the `--json` summary |
| `skills/web-clipper/scripts/inpage.js` | The in-page step Playwright runs before the DOM is serialized |
| `skills/web-clipper/templates/row.json` | Default template, from Obsidian Web Clipper's (see `THIRD-PARTY-NOTICES.md`) |
| `skills/web-clipper/agents/openai.yaml` | Codex display metadata, kept for when Codex is served |
| `tests/` | Offline unit and contract tests; the end-to-end ones skip without the CLI, Node 21+ or Playwright |

## Rules for changes

- The contract is the command, its arguments, exit codes 0-5 and the `--json` keys;
  `ContractTest` pins them. Breaking any is a major version, adding one a minor.
- Callers run the bare `web-clipper`. A path into the plugin breaks: the installed copy lives
  under a cache folder named for its version.
- One version: `VERSION` in `clip.py` equals `version` in `plugin.json` (`PackagingTest`).
  Installed copies update only when it changes. Release: bump both, merge, then
  `claude plugin tag --push` (tag `web-clipper--vX.Y.Z`, which dependents' ranges resolve to).
- User settings live in `~/.config/web-clipper/config.json`; anything the plugin must keep
  between versions goes in `${CLAUDE_PLUGIN_DATA}`, never the plugin folder.
- Claude-only for now, like zkfy. Serving Codex, Pi and Antigravity is planned separately.

## Verify

```bash
python3 -m unittest discover -s tests -v   # Python 3.9+
claude plugin validate .
claude --plugin-dir .                      # a session with this checkout instead of the installed copy
```
