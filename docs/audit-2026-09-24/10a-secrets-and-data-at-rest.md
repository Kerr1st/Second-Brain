# Track 10a — Secrets and Data at Rest (second pass, narrow scope)

Repo: /Users/kerrfir/second-brain @ HEAD 6b8c2d2
Date: 2026-09-24
Mode: read-only; this file is the only write target. No secret values, tokens, emails or personal content are reproduced here — only key names, paths, modes, sizes, structure.

Status: COMPLETE (see roll-up at end)



## 1. Secrets inventory (key names only)

| Secret (key name) | Where it lives | Mode | Gitignored / tracked | Notes |
|---|---|---|---|---|
| `CAPTURE_API_TOKEN` | `/Users/kerrfir/second-brain/.env` (single key; 3-line file, 240 B) | `-rw-------` (0600) | `.gitignore:31` `.env`; `git ls-files` confirms untracked | Loaded by `scripts/jobs/capture_api.sh` (`set -a; . ./.env; set +a`). 64-char hex. |
| Backup encryption key | `/Users/kerrfir/second-brain/.backup-key` (45 B, single line, `openssl rand -base64 32` shape) | `-rw-------` (0600) | `.gitignore:30`; untracked | Used by `scripts/jobs/backup.sh` / `verify_backup.sh` via `KEY_FILE=$HOME/second-brain/.backup-key` (see §4) |
| AWS credentials (Bedrock, SSO) | `~/.aws/config` profile + SSO cache (outside repo) | see §6 | n/a | No AWS keys in repo tree (grep for `AKIA`/`aws_secret_access_key` in tracked files: none — see "Could not verify" for `.venv`/logs) |
| Postgres password | `docker-compose.yml` `POSTGRES_PASSWORD` (see below) | tracked | tracked | Dev-only local DB credential; hardcoded in a tracked file |
| `EXPRESS_EMAIL_TO` / `EXPRESS_EMAIL_FROM` / `GMAIL_APP_PASSWORD` | referenced by code, **absent everywhere** (track 04 confirmed) | — | — | Email push is inert; no secret at rest |

Installed launchd units: `~/Library/LaunchAgents/com.second-brain.*.plist` are **symlinks into the repo `scheduling/`** (15 of 17), so any secret placed in a tracked plist is instantly both tracked and live. Two are regular-file copies (`codex-capture`, `qd-sync`) — not symlinks (track 05 finding 1.2b) — checked below for embedded secrets.

## 2. History — was a secret VALUE ever committed?

Method: `git log --all -S 'CAPTURE_API_TOKEN'`, per-revision scan of every blob of `scheduling/com.second-brain.capture-api.plist` for `[0-9a-f]{32,}`, `git log --all -G '[0-9a-f]{64}'` across `scheduling/ scripts/ src/`, and a scan of unreachable objects (`git fsck --unreachable --no-reflogs`). Values were compared by shell string equality only; nothing printed.

Findings:

- **F2.1 (S2, accuracy):** In **every reachable revision** of `scheduling/com.second-brain.capture-api.plist` (79c44bf 2026-03-28, 8362747, cbe9768 2026-09-14) the `EnvironmentVariables/CAPTURE_API_TOKEN` value is the literal placeholder `REPLACE_WITH_ACTUAL_TOKEN`; no reachable commit on any local ref (`main`, `origin/*`, `mini/*`, 222 `refs/code-defender/scans/*`) contains a 40+-hex token in `scheduling/`, `scripts/` or `src/`. Therefore the statements
  - `docs/MODEL-BACKENDS.md:102` "The retired value remains in git history and should be treated as public", and
  - `tests/test_repo_hygiene.py:3-4` "a live CAPTURE_API_TOKEN was found inline in the tracked launchd plist (and had transited GitHub in since-deleted branches)"
  
  are **not reproducible from reachable local history**. Either the live value was committed on a branch that has since been deleted locally *and* on the remotes (plausible — see F2.2), or the plist was edited in the working tree/installed copy without ever being committed. Fix: reword both to "was present in the working-tree/installed plist and possibly in since-deleted remote branches; not in any reachable commit as of 2026-09-24", or cite the commit hash if the author has it.

- **F2.2 (S1, data at rest):** One **unreachable (dangling) blob** `7f8a361ec751aa52263b0dd467256fe1d47a3a1c` (889 B) in `.git/objects` is a full `com.second-brain.capture-api.plist` carrying `EnvironmentVariables/CAPTURE_API_TOKEN = <64-hex>`. That value **differs from the current `.env` value** (rotation on 2026-09-14 confirmed by inequality). It is not on any ref or reflog, so `git log`/`grep` of history will never show it, but it is still recoverable with `git cat-file` and will survive until `git gc --prune=now` (or `git prune --expire=now`). Because the value is retired this is low-impact today, but it demonstrates that (a) the live token *was* staged/committed at some point (a blob only exists if `git add` or a commit created it), and (b) any future accidental staging leaves the same residue. Fix: run `git reflog expire --expire=now --all && git gc --prune=now --aggressive` once (operational change, not part of this audit), and add a note to `docs/MODEL-BACKENDS.md:102`.

- **F2.3 (S2, repo integrity / refactor seam):** Local `main` (root `9ac1f1a`, head `6b8c2d2`, 2026-09-23) and `origin/main` (root `0dd78c2` "Initial public snapshot" 2026-06-24, head `8c0d58c` 2026-09-06) have **disjoint roots** — `git merge-base --is-ancestor 0dd78c2 main` → no. The five `origin/codex/*` branches and `mini/main` also descend from `0dd78c2`. Local `main` therefore cannot fast-forward to any remote; a push would need `--force` or a fresh remote, and the hygiene test's claim that GitHub history is relevant to *this* repo's history is muddied. Fix: document which root is canonical (`docs/OPERATIONS.md` / `README.md`) and either graft or retire the stale remotes.

- **F2.4 (S3, noise):** 222 refs under `refs/code-defender/scans/tree-*` (a scanner writing refs into the repo). They inflate `git log --all`, `git for-each-ref` and any "scan all history" tooling, and they keep otherwise-dead trees reachable (which, incidentally, is why the rotated plist blob is *not* among them — it was never scanned). Fix: `git for-each-ref refs/code-defender | ...delete` or configure the scanner to use a separate ref namespace outside the repo; add to `.git/info/exclude` guidance in OPERATIONS.


### Where else the token value can be captured

- **F1.1 (S2, secret exposure via agent transcripts):** The *retired* 64-hex token value appears in **14 files under `~/.kiro`/`~/.claude`/`~/.codex`** (Kiro session `messages.jsonl` / `sub-executions/*.jsonl` transcripts — an agent read or printed the pre-rotation plist at some point). The *current* value appears in **zero** files outside `.env` (searched: repo working tree except 18 GB `backups/`, `~/Library/Logs/second-brain`, `~/Library/LaunchAgents`, `~/.kiro`, `~/.claude`, `~/.codex`, `~/.config`, shell rc files). Fix: treat agent transcript directories as a secret sink — add to `docs/MODEL-BACKENDS.md:102` and to the rotation runbook ("after rotating, grep `~/.kiro/sessions` for the old value and purge").
- **F1.2 (S3, hygiene test scope):** `tests/test_repo_hygiene.py:19-20` only scans **tracked `scheduling/*.plist`** for `TOKEN|SECRET|PASSWORD|API_KEY|PASSPHRASE` keys and 40+-hex literals. It does not scan `scripts/jobs/*.sh`, `config/*.toml`, `docker-compose.yml`, `.kiro/**` or the two **non-symlinked installed plists** (`~/Library/LaunchAgents/com.second-brain.codex-capture.plist`, `com.second-brain.qd-sync.plist`; env keys present today: `SECOND_BRAIN_PROFILE`, `AWS_PROFILE`, `CODEX_HOME` — no secrets). Fix: widen `_tracked([...])` to `scripts/**/*.sh`, `config/**`, `docker-compose.yml`.
- **F1.3 (S2, hardcoded credential + network exposure):** `docker-compose.yml:8-10` hardcodes `POSTGRES_USER/PASSWORD/DB = memory_bank` and `src/db.py:19-21` defaults to the same trio (`DB_NAME/DB_USER/DB_PASSWORD` env overrides exist but nothing sets them). `ports: "5432:5432"` (`docker-compose.yml:12`) binds on **all interfaces**, so the entire memory corpus (emails, Slack, browser history, calendar) is reachable from any host on the same network with a guessable password unless the macOS firewall blocks it. Fix: `"127.0.0.1:5432:5432"` and move the password into `.env` (`${DB_PASSWORD}` interpolation), which `capture_api.sh` already sources.
- **F1.4 (S3, info):** No in-repo client sends `Authorization: Bearer …` — `CAPTURE_API_TOKEN` is referenced only by `scripts/jobs/capture_api.sh`, `scheduling/com.second-brain.capture-api.plist` (comment) and `src/capture_api.py`; the capture jobs (`src/capture/*.py`) write to the DB directly. The bearer token therefore protects an endpoint whose only callers are external (Kiro hooks/agents, curl). `docs/ARCHITECTURE.md` is the only doc that mentions `:8100`. Fix: document the intended clients and how *they* obtain the token (otherwise every consumer will keep a private copy → more sinks like F1.1).
- **F1.5 (S3, no logging leak):** `src/capture_api.py:49` logs only `method path status`; `verify_token` (:37-41) uses `hmac.compare_digest` and never logs the header; the launcher does not `set -x`. `logger.exception` at :140/:161/:175/:191 logs tracebacks, which may include request-body memory *content* (personal) but not the token. OK as-is.
- **F1.6 (S2, untracked corpus in a code dir):** `scripts/backups/kb-sources/` — **1,315 plaintext markdown files, 62 MB, mode 0644/0755**, scraped internal wiki/doc pages (file names alone reveal Amazon-internal system names). It is untracked only because the unanchored `.gitignore:32` pattern `backups/` happens to match a sub-directory named `backups` (`git check-ignore -v` → `.gitignore:32:backups/`). Track 05 finding 6c flagged the unanchored pattern; this is the concrete consequence: a rename of the directory or an anchored fix (`/backups/`) would make 62 MB of internal content trackable in one `git add -A`. Fix: move to `staging/` or `~/second-brain-data/`, and anchor the pattern *after* moving.


## 3. Personal content at rest outside the DB

All paths below are gitignored (`.gitignore:31-33`, `:11`, `:14`, `:27`) and untracked — confirmed with `git ls-files`. Sizes: `logs/` 109 MB (314 entries), `backups/` 18 GB, `staging/` 588 KB, `tests/fixtures/` 488 KB, `evaluations/` 8 KB, `.dream/` 8 KB, `~/Library/Logs/second-brain` 8 KB, `scripts/backups/` 62 MB.

| Location | What is there (structure only) | Personal content? | Mode |
|---|---|---|---|
| `logs/llm_metrics/*.jsonl` (17 files, 11 MB; `adhoc.jsonl` alone 11.1 MB, 26,949 records) | Keys: `ts run_id stage agent_name model effort tools timeout_s system_prompt_chars user_message_chars input_chars output_chars success error latency_s est_*_tokens real_*_tokens usage_source total_cost_usd backend duration_s model_honored mcp_startup_retries`. **No `prompt`, `output`, `content`, `completion`, `response`, `messages`, `text` keys in any file.** `error` is a short code (`timeout`, `json_parse_failed`, `exit_code=N`; max length 17 chars). | **No** — metrics only | 0644 |
| `logs/dream-cycle-digest-*.md` (16 files, 5,239 lines) | Sections `Run Statistics / Feedback Loop Summary / Explorer Strategies Used / Accepted Insights` with per-insight `WHAT / WHY IT MATTERS / EVIDENCE`. 1,415 lines > 200 chars = synthesized insight prose derived from the corpus. 0 email-shaped tokens, 2 URLs. | **Yes (derived)** — insight text about the user's projects/people | 0644 |
| `logs/capture-slack-*.log` (1,580 lines, JSON) | Keys `timestamp level logger message metadata channelId duration operation error`. Messages are operational (`get_messages completed`, `listMyChannels: …`). **749 lines carry a Slack `channelId`**; no message text, user names or permalinks. | Identifiers only | 0644 |
| `logs/capture-browser-*.log` (122 lines) | 10 lines `⚠ Skip (too short): <URL>` + 1 Crawlee warning line — **12 real browsing URLs** (hosts across 6 domains incl. video/learning/productivity SaaS) written in plaintext by the skip path. | **Yes** — browsing history leaks into an ops log | 0644 |
| `logs/capture-outlook-*.log` (356 lines) | Window/traceback lines; no subject/from fields found. | No | 0644 |
| `logs/qd-sync-*.log` (25 files, 4–8 MB each ≈ 100 MB) | ~791 traceback frames + 114× `SSO token registration expired` per file — repeated botocore `TokenRetrievalError` stack traces. No content. | No — but this is the unprunable append log from track 05 | 0644 |
| `logs/skim_probe/` (1 file, 4 KB) | `docs/MODEL-BACKENDS.md:100` says it "contains document-derived text". | **Yes (per docs)** | 0644 |
| `logs/panel_replay/` (240 KB), `logs/explorer_raw_output.txt` (25 KB) | Raw agent output captures | Likely yes (agent output over corpus) | 0644 |
| `tests/fixtures/golden_run/*.json` (35 files, 392 KB) | `{inputs:{system_prompt, user_message}, output}` — full prompts (5.7 KB system, 1.7 KB user per thinker) and completions. File names are memory titles. 0 emails, 0 URLs. **Untracked** (`.gitignore:11`). | **Yes** — real memory titles + prompt text; correctly excluded | 0644 |
| `tests/fixtures/codex/real_*.json{,l}` (7 files, 18 KB) | **Tracked.** Codex session records: `timestamp type payload{session_id id timestamp cwd originator source thread_source model_provider …}`. Contains: 1 email-shaped token (3-char local part, non-corporate domain), 2 corporate hostnames, and an absolute `/Users/<account>/…` home path in `cwd` (5 files). | Low — real session IDs + home path + one address committed to git | tracked |
| `staging/` (588 KB) | transient ingest staging | Yes (raw captures in transit) | 0755 dir |
| `backups/*.gpg` (40 files) | 8 days × {`memory_bank_*.dump`, `memories_*.json`, `entities_*.json`, `edges_*.json`, `memory_entities_*.json`}`.gpg`, ≈1.25 GB per dump | Full corpus — **encrypted** (see §4) | **0644** |
| `backups/config/` (56 entries incl. `kiro-skills/` 377 files) | Plain copies of `docs/*.md` (top-level, **including the uncommitted `AUDIT-2026-09-23.md`**), `migrations/*.sql`, `docker-compose.yml`, `requirements.txt`, README, `~/.kiro/skills/**` | Internal docs, **plaintext** | 0644 |
| `backups/kb-sources/` (1,358 files) and stale twin `scripts/backups/kb-sources/` (1,315 files, 62 MB) | rsync mirror of `~/Work/Tools/Crawlee/knowledge/sources/` — scraped internal wiki/docs pages | **Yes — internal content, plaintext** | 0644 |
| `evaluations/` , `.dream/` | 8 KB each; query sets / dream state; no content found | No | 0644 |
| `~/Library/Logs/second-brain/` | 8 KB launchd stdout/stderr | No | 0644 |

Findings:

- **F3.1 (S2, defect):** `src/capture/browser.py` "Skip (too short)" path prints the **full URL** into `logs/capture-browser-*.log` (10 of 12 URL occurrences). `browser.py:92` strips secret query params for storage, but the skip log line is emitted before/without that stripping (evidence: 12 URLs present in a 122-line log). Fix: log the URL's host only, or the memory id, at the skip site.
- **F3.2 (S2, inaccuracy):** `.gitignore:11` comment "Golden run fixtures (contain full prompts and real memory content)" is **correct**; but `tests/` that depend on `tests/fixtures/golden_run/` will silently skip or fail on a fresh clone since the directory is never tracked (35 files exist only on this machine). Track 12a/12c own the test side; from the data side, note that regenerating the fixtures re-embeds real corpus text in a path that a single `.gitignore` edit would expose. Fix: generate golden fixtures from synthetic memories, or store them outside the repo tree.
- **F3.3 (S3, tracked PII-adjacent):** `tests/fixtures/codex/real_*.json{,l}` commit real Codex `session_id`s, a home-directory path and one e-mail address. Fix: scrub `cwd` → `/home/user/project`, replace the address with `user@example.com`, and regenerate UUIDs.
- **F3.4 (S2, stale copy):** `scripts/backups/` is a **stale duplicate** of `backups/{config,kb-sources}` (81 file-name differences), left from a pre-`7db3a6c` `backup.sh` that resolved `BACKUP_DIR="backups"` relative to `scripts/`. Nothing references it. Fix: delete `scripts/backups/` (62 MB of internal content sitting in a *code* directory).
- **F3.5 (S3):** All artefacts above are mode 0644 under a 0755 tree; on a single-user Mac this is acceptable, but `backups/*.gpg` (0644), `logs/dream-cycle-digest-*.md` and `staging/` would be readable by any other local account. Fix: `umask 077` at the top of `backup.sh`, `job_wrapper.sh`, or `chmod 700 backups logs staging`.
- **F3.6 (S3, docs):** `docs/MODEL-BACKENDS.md:81` "Metrics are JSONL in `logs/llm_metrics`" — verified: metrics carry sizes/counts only, never prompt or completion text. This is a **positive** confirmation worth stating explicitly in `docs/MODEL-BACKENDS.md` (it currently doesn't say what is *not* logged).

## 4. Backup security

Source: `scripts/jobs/backup.sh` (125 lines), `scripts/jobs/verify_backup.sh` (77 lines), `docs/DISASTER-RECOVERY.md`. The DR doc (source-reviewed 2026-09-10) already correctly states: plain-file sync of kb-sources/config, `set -uo` without `-e`, `verify` only proves `pg_restore -l` readability. Not repeated below; the following is additional.

- **Encryption:** `backup.sh:68` `gpg --symmetric --cipher-algo AES256 --batch --yes --passphrase-file "$KEY_FILE"`. Passphrase = one line of `openssl rand -base64 32` (45 B incl. newline) in `/Users/kerrfir/second-brain/.backup-key`, mode 0600, gitignored. GPG symmetric uses S2K on the passphrase; with a 256-bit random passphrase this is adequate. Integrity: OpenPGP MDC only — no separate signature/HMAC, so a tampered ciphertext is detected only at decrypt time (which `verify_backup.sh` does perform).
- **F4.1 (S1, key custody — single point of failure):** The only copy of the decryption key found is `.backup-key` on this machine. `docs/DISASTER-RECOVERY.md:32` defers to "the user's private custody process" and the asset table says "Verify independent key custody privately". If the machine (or just this file) is lost, **all 40 local and all Google-Drive `.gpg` backups are unrecoverable**, i.e. the off-site copy has zero DR value without an off-site key. Could not verify whether a copy exists in a password manager/keychain (not inspectable read-only). Fix: document the escrow location (name only) in `DISASTER-RECOVERY.md`, and have `verify_backup.sh` fail if a keychain entry named e.g. `second-brain-backup-key` is absent (`security find-generic-password -s … >/dev/null`).
- **F4.2 (S1, internal content to a third party, plaintext):** `backup.sh:92-93` `rclone sync backups/kb-sources/ gdrive:memory-bank-backups/kb-sources/` and `…/config/` push **1,358 scraped internal wiki/doc pages** and `docs/*.md` (which now includes the audit reports) plus 377 `~/.kiro/skills` files to Google Drive **unencrypted**. The DR doc acknowledges "as plain files" but frames it as a completeness limitation, not as a confidentiality issue. Fix: either tar+gpg those two trees with the same key before upload, or drop them from the cloud copy (they are re-derivable: Crawlee sources from their origin, docs from git).
- **F4.3 (S2, over-privileged cloud token):** `~/.config/rclone/rclone.conf` (mode 0600, good) `[gdrive]` has `scope = drive` — full read/write over the **entire** Google Drive, not `drive.file` (app-created files only). A leak of that OAuth token exposes everything in the account, not just `memory-bank-backups/`. Fix: re-authorize the remote with `scope = drive.file`.
- **F4.4 (S2, plaintext window & residue):** `backup.sh:44-60` write five plaintext files (≈1.25 GB dump + JSON exports of every memory) into `backups/` with default umask (0644) and only encrypt+`rm` them at :62-70. If the job is killed between :44 and :70 (launchd timeout, reboot, disk full) the plaintext stays; `.gitignore` `*.dump`/`backups/` keeps it out of git and `--include "*.gpg"` keeps it off Drive, but it sits on disk indefinitely (no cleanup of `*.dump`/`*.json` at :118 — only `*.gpg -mtime +7`). Today: 0 plaintext files present. Fix: `trap 'rm -f "$BACKUP_DIR"/*_${TIMESTAMP}.{dump,json}' EXIT`, pipe `pg_dump … | gpg … > file.gpg` to avoid the plaintext entirely, and `umask 077`.
- **F4.5 (S2, verify proves less than it logs):** `verify_backup.sh:34` picks the newest `*.dump.gpg` on Drive, decrypts to fixed `/tmp/second-brain-verify/test.dump` (world-readable dir, default umask → a ~1.25 GB plaintext corpus in `/tmp` for the duration), runs `pg_restore -l` (TOC only) and counts `TABLE DATA` entries. It never verifies the four `*.json.gpg`, never checks the **local** copy, never compares the row count to `backup.sh:45`'s `$ROWS`, and never checks that the "latest" is from the last 24-48 h — a stale Drive copy passes forever. `rm -rf "$VERIFY_DIR"` at :70 runs even on failure paths (good). Fix: `mktemp -d` with `umask 077`; assert the timestamp in `$LATEST` is < 2 days old; decrypt each of the five artefacts; log `TABLE DATA` count *and* compare against a committed expected table list.
- **F4.6 (S3):** `backup.sh:5` `set -uo pipefail` (no `-e`) + `rm "$f"` at :69 unconditional → if `gpg` fails (e.g. key file unreadable after a mode change) the plaintext is **deleted and no `.gpg` exists**, yet `GDRIVE_OK` stays true because `rclone copy` of zero new files succeeds → "Backup complete". Already flagged in DR doc as "needs repair"; still unrepaired at HEAD. Fix: `gpg … && rm "$f" || { log "encrypt failed"; exit 1; }`.
- **F4.7 (S3, drift):** `backup.sh:25-27` falls back to `brew services start postgresql@17` while `docker-compose.yml` still defines the DB container and `DB_CONTAINER` (:12) is read but never used — two DB hosting models documented in the same script. `backup.sh:3` says "durable copies = local + GDrive + git" but `rclone.conf` still carries an `[s3]` remote (`env_auth`, region) that nothing uses. Fix: delete `DB_CONTAINER`, drop the `[s3]` remote or document it as dormant.
- **Google Drive destination:** `gdrive:memory-bank-backups/` (root), `…/kb-sources/`, `…/config/`; retention `rclone delete --min-age 30d --include "*.gpg"` (:117) — note the plain `kb-sources/`/`config/` trees are `sync`ed (mirrored, so deletions propagate) and never age out.


## 5. Dependencies

`requirements.txt` (18 lines): **7 exact pins** (`psycopg2-binary==2.9.11`, `boto3==1.42.69`, `mcp==1.26.0`, `browser-cookie3==0.20.1`, `requests==2.32.5`, `hypothesis==6.151.9`, `pytest==9.0.2`, `yt-dlp==2026.3.17`, `curl_cffi==0.13.0`) and **4 open ranges** (`fastapi>=0.115.0`, `uvicorn[standard]>=0.34.0`, `numpy>=2.0.0`, `scipy>=1.14.0`). `requirements-dev.txt`: `-r requirements.txt` + `pytest-cov==7.1.0`. No lock file (`uv.lock`/`poetry.lock`/`pyproject.toml` absent). Installed in `.venv` today: fastapi 0.136.3, uvicorn 0.49.0, numpy 2.4.6 — i.e. the open ranges have drifted far past their floors and are not reproducible.

`uvx pip-audit -r requirements.txt` (network, read-only; run with `--python .venv/bin/python` because the default `uvx` interpreter lacks the `venv` module) → **21 known vulnerabilities in 5 packages** (IDs de-duplicated):

| Package | Pinned | Advisory IDs | Fixed in |
|---|---|---|---|
| `mcp` | 1.26.0 | PYSEC-2026-3481, PYSEC-2026-3482, PYSEC-2026-3483 | 1.27.2 / 1.28.1 |
| `requests` | 2.32.5 | PYSEC-2026-2275 | 2.33.0 |
| `pytest` | 9.0.2 | PYSEC-2026-1845 | 9.0.3 |
| `yt-dlp` | 2026.3.17 | PYSEC-2026-3430, PYSEC-2026-3431, PYSEC-2026-3433, PYSEC-2026-3622, GHSA-69qj-pvh9-c5wg | 2026.6.9 / 2026.7.4 |
| `curl-cffi` | 0.13.0 | PYSEC-2026-2431 | 0.15.0 |

- **F5.1 (S1):** `mcp==1.26.0` carries three advisories and is the **network/stdio surface** of the memory server (`docs/MODEL-BACKENDS.md:102` already notes it has no per-tool authorization). Fix: bump to ≥1.28.1 and re-run `tests/test_mcp*`.
- **F5.2 (S2):** `yt-dlp`/`curl_cffi` are pinned together by the comment at `requirements.txt:13-16` (yt-dlp 2026.3.17 rejects curl_cffi 0.15.0). Both have open advisories and the fix for `curl_cffi` (0.15.0) is exactly the version the comment says is rejected — the pair must be bumped **together** to yt-dlp ≥ 2026.7.4 and then `curl_cffi` re-checked against its new support matrix. These process untrusted remote media/HTML, so this is not cosmetic.
- **F5.3 (S3):** `requests` and `pytest` fixes are patch bumps.
- **F5.4 (S3, reproducibility):** The four open ranges + no lock file mean `backups/config/requirements.txt` (copied by `backup.sh:81`) cannot reproduce the environment during a restore — contradicting `DISASTER-RECOVERY.md` step 2 "recover the matching source tree and requirements". Fix: `pip freeze > requirements.lock` in `backup.sh`, or adopt `uv lock`.

## 6. AWS least privilege (names only; no STS/IAM calls made)

- Profile used by unattended jobs: **`second-brain-jobs`** — set by `scripts/jobs/job_wrapper.sh:13` (`export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"`), the two non-symlinked installed plists (`codex-capture`, `qd-sync` → `<key>AWS_PROFILE</key>`), and 6 other `scripts/jobs/*.sh` sites.
- `~/.aws/config` (mode 0600): `[profile second-brain-jobs]` contains **only `region = us-east-1`** — no `sso_*`, no `role_arn`, no `credential_process`. Credentials therefore come from `~/.aws/credentials` (mode 0600, 168 B), whose `[second-brain-jobs]` section holds a **static IAM-user access-key pair** (`aws_access_key_id` with the long-lived `AKIA` prefix + `aws_secret_access_key`), last modified 2026-09-17. Header comment in that file says "AWS credentials are managed through SSO" — **contradicted by its own contents**.
- Interactive profiles `default`, `internal-ide`, `kiro-ide`, `qdeveloper-ide` are Identity Center SSO with `sso_role_name = AdministratorAccess`; `verify_credentials.sh:9,25-28` correctly classifies `default` as ADVISORY/human-only and `second-brain-jobs` as REQUIRED.
- Least-privilege claim: `job_wrapper.sh:11` and `verify_credentials.sh:6` say "least-privilege IAM user (Bedrock InvokeModel only)"; `docs/RESEARCH-skim-model.md:50-51` names the policy `BedrockInvokeOnly` (InvokeModel + InvokeModelWithResponseStream on `foundation-model/*` and inference profiles) and :516 notes `GetFoundationModelAvailability` is *denied* — consistent with a narrow policy. **Could not verify** the attached policy (IAM calls prohibited by the brief); the profile *name* and docs look read/invoke-only, not admin.
- **F6.1 (S2, credential hygiene):** A long-lived static IAM key is the weakest credential type in the setup (no expiry, no MFA, no session). The doc trail explains why (8 h SSO killed unattended jobs on 2026-09-08). Fix (operational): rotate on a schedule (`verify_credentials.sh` could warn when `~/.aws/credentials` mtime > 90 d), or move to IAM Roles Anywhere / an EC2-hosted runner. Not a code change.
- **F6.2 (S1, plaintext credential residue):** `~/.aws/credentials.backup` is **mode 0644 (world-readable)** and contains **one `aws_secret_access_key` line**; `~/.aws/config.backup`, `config.backup.20260324101255`, `config~` are also 0644 (SSO/`AdministratorAccess` role metadata only, no secrets). Whether the key in `credentials.backup` is still active could not be verified (no IAM calls). Fix: `chmod 600` immediately, then delete the `*.backup`/`*~` files after confirming the key is rotated/deactivated in IAM.
- **F6.3 (S3, docs drift):** `verify_credentials.sh:6` says the IAM user serves "embeddings + Astra"; `docs/MODEL-BACKENDS.md` describes Bedrock for skim/thinker/evaluator too. Fix: align the one-liner.

## Could not verify

- Whether the retired token value was ever pushed to GitHub in since-deleted branches (`tests/test_repo_hygiene.py:4`) — no local ref contains it; only the dangling blob `7f8a361` does.
- Whether a second copy of `.backup-key` exists in a password manager/keychain (F4.1) — not inspectable without invoking `security`/1Password.
- Whether the `AKIA` key in `~/.aws/credentials.backup` (0644) is active, and the exact policy attached to the `second-brain-jobs` IAM user — IAM/STS calls were out of scope.
- Contents of the 18 GB `backups/*.gpg` (encrypted; not decrypted) and of `~/Library/Logs/second-brain` beyond size/mode.
- Whether `logs/skim_probe`, `logs/panel_replay`, `logs/explorer_raw_output.txt` contain PII — only sizes/presence checked, contents deliberately not read.
- Whether macOS Application Firewall blocks inbound 5432 (F1.3) — not queried.
- pip-audit results reflect the PyPI advisory DB at run time (2026-09-24); the `uvx` default Python (3.12) is broken (`No module named 'venv'`), which is why the audit needed `--python .venv/bin/python`.

## Severity roll-up

- **S1:** F2.2 (live token blob still in `.git/objects`, retired), F4.1 (single-copy backup key), F4.2 (1,358 internal pages + docs to Google Drive in plaintext), F5.1 (`mcp` 1.26.0 three advisories), F6.2 (world-readable AWS credential backup).
- **S2:** F1.1, F1.3, F1.6, F2.1, F2.3, F3.1, F3.2, F3.4, F4.3, F4.4, F4.5, F5.2, F6.1.
- **S3:** F1.2, F1.4, F1.5, F2.4, F3.3, F3.5, F3.6, F4.6, F4.7, F5.3, F5.4, F6.3.

Status: COMPLETE
