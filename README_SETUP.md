# IT-bot admin bot: setup (web clicks only, no terminal)

## Two repos, never one
| Repo | Holds | Visibility |
|---|---|---|
| `IT-bot-admin` (code) | the files in this folder | private (recommended) |
| `IT-bot-admin-data` (data) | `admin.db`, lecture text, backups | **PRIVATE** (lectures are your professors' material) |
The bot commits to the data repo often. Render must watch ONLY the code repo, or every save redeploys the bot.

## Steps
1. **Data repo → private:** GitHub → your data repo → Settings → scroll to *Danger Zone* → *Change visibility* → Private.
2. **Code repo:** New repository `IT-bot-admin` (private). Click *uploading an existing file*, unzip `itbot-admin.zip`, drag ALL files in, Commit.
3. **Telegram bot:** BotFather → `/newbot` → copy the token (keep it for step 6 only).
4. **GitHub token:** github.com → profile photo → Settings → Developer settings → Personal access tokens → *Tokens (classic)* → Generate new token (classic) → name `itbot-admin`, expiration 30 days, tick **repo** → Generate → copy it once.
   (Fine-grained tokens can't select a repo owned by someone else, so classic is needed for the live repo.)
5. **Render:** New → Web Service → connect `IT-bot-admin` → Runtime **Docker** → Free plan.
6. **Environment variables** (Render → Environment):
   | Name | Value |
   |---|---|
   | `BOT_TOKEN` | token from step 3 |
   | `GITHUB_TOKEN` | token from step 4 |
   | `DATA_REPO` | `your-username/IT-bot-admin-data` |
   | `ADMIN_IDS` | (step 8) |
   | `LIVE_REPO` | `nadeembrijawy/IT-bot` (default) |
   | `LIVE_BRANCH` | `main` (default) |
7. Deploy. Then free tier sleeps after 15 min: add a free UptimeRobot HTTP monitor on your Render URL (every 5 min), same trick as the live bot.
8. Open the bot in Telegram → `/start` → it shows your numeric id → put it in `ADMIN_IDS` → Render restarts.

## First test (use ONE question)
1. `/subject` → pick a subject.
2. Forward ONE solved quiz poll → summary says "polls carrying an answer: 1" or 0. This is the milestone-0 check.
3. Send one lecture PDF with caption `L1` (add `ocr` for scans).
4. `/bundle` → upload the file to Claude with: *solve the attached*.
5. Send Claude's `patch.json` to the bot → `/review` → `/publish` → Confirm.
6. Check the question in the live bot (Render redeploys it).

## Commands
`/subject` `/lectures` `/drop L5` `/bundle [n] [L5 L6|all]` `/review` `/publish` `/undo` `/stats`
Send `bot.py` → update script (syntax check, diff, confirm). `/rollback_script` restores the last backup.

## Safety
- Only ids in `ADMIN_IDS` can use it. Tokens live only in Render variables.
- Publish: fetches the current `bot.db`, backs it up, commits with the file's SHA (fails safely if it changed), reads back and verifies.
- Never paste a token in chat. If one leaked: GitHub token page → Delete; BotFather → `/revoke`.

## Known limits
- Questions that need an image can't be published (use TODO; add those by hand).
- 20 MB per file (Telegram bot limit).
- Not tested against real Telegram/GitHub yet: your first run is the live test.
