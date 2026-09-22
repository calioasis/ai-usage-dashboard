# AI Usage Dashboard

A static dashboard for personal Claude / Codex (ChatGPT) / (eventually Grok) token
usage, built from data that already exists on this machine — no API keys, no
backend.

## How it works

- `collect_usage.py` reads local session logs:
  - `~/.claude/projects/**/*.jsonl` (Claude Code transcripts — each message logs
    real token counts)
  - `~/.codex/sessions/**/*.jsonl` and `~/.codex/archived_sessions/*.jsonl`
    (Codex CLI rollouts — each turn logs token counts *and* your rolling
    subscription-quota `used_percent`, since ChatGPT/Codex plans are rate-limited,
    not billed per token)
- It writes **only aggregates** (dates, models, project names, token counts,
  estimated cost) to `data/usage-data.json`. Raw prompt/response content is
  never read into that file.
- `index.html` is a static page that fetches `data/usage-data.json` and renders
  it with Chart.js. No server, no live API calls from the browser.

Claude cost figures are **API-list-price equivalents** computed from token
counts — not actual billed spend if you're on a Pro/Max subscription rather
than pay-per-token API billing. Codex has no per-token cost shown; instead the
dashboard tracks `rate_limits.used_percent`, which is the actual thing that
determines when you hit a wall on a subscription plan. The "heaviest sessions"
table exists specifically to catch a single long-lived/runaway session before
it burns a whole day's quota.

## Refreshing the data

```bash
./refresh.sh
```

This regenerates `data/usage-data.json`, commits it, and pushes. Cloudflare
Pages (once connected, see below) redeploys automatically on push.

A daily automated refresh is set up via launchd — see `com.jeffsherin.ai-usage-dashboard.plist`.

## One-time hosting setup (Cloudflare Pages)

1. Push this repo to GitHub (already done if you're reading this from the repo).
2. In the Cloudflare dashboard: **Workers & Pages → Create → Pages → Connect to Git**.
3. Select this repo. Build settings: no build command, output directory `/`
   (it's plain static HTML/JS, same as `family_dashboard`).
4. Deploy. Cloudflare gives you a `*.pages.dev` URL; attach a custom domain if
   you want one.
5. From then on, every `git push` (including from `./refresh.sh`) triggers an
   automatic redeploy.

## Adding Grok later

`collect_usage.py` already writes a `"grok": null` placeholder into the JSON
and `index.html` has a placeholder card. When you're ready: xAI's console
(console.x.ai) meters usage like OpenAI's platform API — add a small function
that reads an xAI usage endpoint with a key kept in a local, gitignored env
file (never in this repo, never in the page's client-side JS, since this repo
is public).

## Security note

This script never needs, reads, or stores an API key — it only reads log files
that already exist locally. If a future data source (e.g. Anthropic's Admin
usage API, or xAI) is added, keep the key in a local untracked file and make
sure the *key* never ends up in `data/usage-data.json` or `index.html` — only
the resulting numbers should.
