# job-hunt-copilot

[![tests](https://github.com/carloslopezparga/job-hunt-copilot/actions/workflows/tests.yml/badge.svg)](https://github.com/carloslopezparga/job-hunt-copilot/actions/workflows/tests.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)

A **human-in-the-loop** internship search assistant for an industrial engineering student
(ETSII-UPM, Madrid, EU citizen) looking for remote internships now and summer/off-cycle 2027
internships in **AI/tech, quant/trading, investment banking, consulting and fintech**.
Location priority: **Madrid → rest of Spain / remote from Spain or the EU → EU cities (no visa)
→ the rest of the world only where a visa is not a blocker**. Applications are prepared in
**English** (default) and **Spanish**.

It finds postings on public job boards, scores them against your profile, and prepares a
tailored **CV (PDF)**, a **150–200 word cover letter** and a **LinkedIn note (≤ 300 chars)**
for each good match. It tracks your pipeline, reminds you to follow up after 7 days, and can
run every day on Windows and ping you on **Telegram** with the new matches.

> [!IMPORTANT]
> **The copilot never applies for you.** It does not submit forms, send emails or messages to
> employers, or automate LinkedIn in any way (no scraping, no clicks, no bots). It only reads
> **public, documented job-board APIs**, writes files to your disk and (optionally) messages
> **your own** Telegram chat. **You** review every document, decide whether to apply, and send
> it yourself.
>
> **It never invents or embellishes anything about you.** Every sentence in your CV, letters
> and messages is assembled from facts you wrote in `profile.yaml`. If your profile is too
> thin for a 150-word letter, you get a warning, not filler. An optional LLM can rephrase
> text, but a fact guard throws away any output that adds numbers, names or links.

---

## How it works

```mermaid
flowchart LR
    C[companies.yaml] --> D
    subgraph sources
      D[discover] --> GH[Greenhouse API]
      D --> LV[Lever API]
      D --> AS[Ashby API]
    end
    M[jobhunt add<br/>manual offers] --> T
    D -->|normalized + deduplicated jobs| T[(SQLite tracker)]
    P[profile.yaml] --> S[score]
    T --> S -->|0-100 + one-line why| T
    T --> R[run: new since last run<br/>+ location filters]
    R --> G[prepare]
    P --> G
    G --> O["output/ID-company-role/<br/>cv.pdf · cover_letter.txt · linkedin_note.txt · job.md"]
    R --> TG[Telegram: your own chat]
    O -.->|you review & apply yourself| U((You))
    TG -.-> U
    U -->|jobhunt status id applied| T
    T --> F[followups after 7 days]
```

| Module | What it does |
| --- | --- |
| `profile.yaml` | Your single source of truth: education, experience, projects, languages (real CEFR level + certificate only if you hold it), skills, sector motivations, role/sector/location/date preferences and work authorization. Git-ignored. |
| `src/jobhunt/sources/` | Connectors for the public **Greenhouse**, **Lever** and **Ashby** job-board APIs behind one `JobSource` interface; `check.py` verifies boards before you rely on them. |
| `src/jobhunt/location.py` | Splits multi-location strings, recognises cities/countries/regions (EN/ES), remote/hybrid markers and whether a place needs a visa you lack; ranks each job into location tiers. |
| `src/jobhunt/scoring.py` | Scores each job 0–100 with configurable weights and a one-line explanation; filters duplicates; re-scores automatically when the profile changes. |
| `src/jobhunt/generator/` | Tailors your profile to the job (ranks *your* bullets/projects/skills), renders Jinja2 templates (EN/ES) and a reportlab PDF CV. Optional LLM polishing; works fully without one. |
| `src/jobhunt/tracker.py` | SQLite pipeline: `new → prepared → applied → follow_up → interview / rejected`, full history, 7-day follow-up reminders, CSV export. |
| `src/jobhunt/notify.py` | Telegram notifications (your own bot and chat; credentials only from environment variables). |
| `src/jobhunt/cli.py` | Typer CLI: `init`, `discover`, `check-boards`, `add`, `score`, `locations`, `prepare`, `run`, `status`, `followups`, `export`. |

## Installation

Requires **Python 3.12+**.

```bash
git clone https://github.com/carloslopezparga/job-hunt-copilot.git
cd job-hunt-copilot
python3.12 -m venv .venv && source .venv/bin/activate     # Windows: py -3.12 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"          # add ,llm for optional LLM polishing: ".[dev,llm]"
```

## Try it offline in 30 seconds

The repo ships realistic API fixtures for three **fictional** companies and a **fictional**
example profile, so you can see the whole pipeline without network access or personal data:

```bash
export JOBHUNT_PROFILE=profile.example.yaml
export JOBHUNT_COMPANIES=tests/fixtures/companies.sample.yaml
export JOBHUNT_TODAY=2026-10-07          # pin "today" so the fixtures' dates make sense

jobhunt run --fixtures tests/fixtures --prepare --export shortlist.md
```

## Usage

### 1. Create your profile

```bash
jobhunt init            # copies profile.template.yaml -> profile.yaml (git-ignored)
```

Fill in every field marked `TODO`. The template explains each one. A few rules:

- **Write only what is true** and you can defend in an interview. Numbers only if you measured them.
- **Languages**: `level` is your real CEFR level; `certificate` only if you actually hold it
  (e.g. `Cambridge C1 Advanced (2022)`). Leave it empty otherwise.
- **Bilingual text**: any free-text field can be `"text"` or `{en: "...", es: "..."}`. Spanish
  letters never mix languages: untranslated sentences are left out (and reported).
- **Work authorization**: `personal.work_authorization: ["EU"]` means EU + EEA + Switzerland.
  Add `UK`, `US`… only if you really can work there without sponsorship.
- `jobhunt prepare` / `jobhunt run --prepare` **refuse to generate anything while any `TODO`
  remains**, so a half-filled template can never end up in front of a recruiter.

### 2. Discover offers and keep the company list healthy

```bash
jobhunt discover                       # all boards in companies.yaml
jobhunt discover -c Stripe -c Monzo    # just a few
jobhunt check-boards                   # 404s, empty boards, postings in Spain / EU per board
```

`companies.yaml` lists the employers that are read on every run (ATS, board slug, sector).
`companies.candidates.yaml` lists **50 more employers with Spain/EU roles that are not active
yet**: each slug was taken from a public board URL (cited in the entry) but still has to be
verified against the live API. Verify them and append only the ones that work and have
postings an EU citizen can take without a visa:

```bash
jobhunt check-boards --file companies.candidates.yaml --promote companies.yaml
jobhunt check-boards --file companies.candidates.yaml --min-eu-jobs 3   # stricter
```

Investment banks and MBB consultancies recruit through Workday/Oracle/in-house portals with
no public API (Madrid off-cycle programmes at Citi, BofA, Barclays, Goldman, Nomura, Alantra,
Arcano…). Track those offers manually (paste the description into a file):

```bash
jobhunt add --company "Example Bank" --title "IBD Off-Cycle Analyst 2027" \
  --url "https://careers.example.com/123" --sector investment_banking \
  --location "Madrid, Spain" --workplace onsite --description-file desc.txt
```

### 3. Score

```bash
jobhunt score                                 # scores new jobs, lists the best ones with an explanation
jobhunt score --no-visa --location Madrid     # filters: --location TEXT, --no-visa, --remote-only, --limit N
jobhunt score --location "Remote - EU" --remote-only --limit 10
jobhunt score --rescore                       # force; done automatically when profile.yaml changes
jobhunt locations                             # how every location string in your tracker is classified
```

Scores are stored in the tracker together with a fingerprint of `profile.yaml` (and of the
scoring rules): when either changes, the next `score`/`run` re-scores every job.

| Component | Default weight | How it is judged |
| --- | ---: | --- |
| `role` | 20 | Your `preferences.roles` keywords in the title (1.0) or only in the description (0.4) |
| `contract` | 20 | Internship detected from the ATS field or title (intern, off-cycle, summer analyst, prácticas, werkstudent…) |
| `location` | 20 | Location tier, see below |
| `skills` | 15 | Your skills/tags found in the posting (5 matches = full marks) |
| `sector` | 10 | Company sector (from `companies.yaml`) is in your `preferences.sectors` |
| `dates` | 10 | "Summer 2027", "June 2027", "off-cycle 2027", "ASAP" (EN/ES)… matched against your availability windows |
| `remote` | 5 | Remote jobs fit everything; on-site jobs only fit availability windows without `remote_only` |

**Location tiers.** `preferences.locations` has three lists (`preferred`, `acceptable`,
`avoid`). Entries can be cities (`Madrid`), countries (`Spain`, `España`), regions (`EU`,
`Europe`, `EMEA`), `Remote`, or a remote/hybrid place (`Remote - Spain`, `Remote - EU`,
`Hybrid - Madrid`). Job locations such as `Madrid; Remote - EU`, `Spain/España`,
`New York, NY | Barcelona, Spain` or `Madrid, London` are split, and **a job is ranked by the
best place it offers**:

| Tier | Example (default profile) | Location component | Final-score multiplier |
| --- | --- | ---: | ---: |
| preferred | Madrid, Hybrid - Madrid | 1.0 | × 1.00 |
| acceptable | Barcelona, Remote - Spain, Remote - EU, Amsterdam, Paris | 0.7 | × 0.95 |
| unknown | no usable location | 0.4 | × 0.90 |
| other | Zurich, EMEA, plain "Remote" | 0.3 | × 0.80 |
| avoided | anything in `avoid` | 0.1 | × 0.60 |
| **visa** | every listed place needs a permit you lack (London, New York, Remote - US, Singapore…) | 0.0 | **capped at 30** |

Titles containing an excluded keyword (`senior`, `staff`, `lead`…) or a past season
("Summer 2026") are capped at 15. Weights live in `profile.yaml → scoring.weights`.
Duplicates (same company + normalized title + city, e.g. a re-posted job) are dropped.

> If your `profile.yaml` was created from an older template it still has `location: 5` in
> `scoring.weights`; raise it to `20` (or delete the `weights` block to use the defaults).
> The tier multiplier and the visa cap apply either way.

Example explanation:

```
91 · internship ✓ · role 'strategy' ✓ · fintech ✓ · junio 2027 fits 'Summer / off-cycle internship 2027'
   · on-site OK · skills: SQL, Excel · Madrid, Spain (preferred)
```

### 4. Prepare material (and review it!)

```bash
jobhunt prepare 12 15          # specific jobs
jobhunt prepare --top 5        # the 5 best "new" jobs above scoring.min_score_to_prepare
jobhunt prepare 12 --lang es   # force Spanish (default: Spanish postings -> es, else en)
```

Each job gets a folder like `output/0012-company-role/`:

| File | Content |
| --- | --- |
| `cv_en.pdf` | One-page CV: your relevant bullets first within each role, most relevant projects, job keywords in **bold**, matching skills listed first |
| `cover_letter_en.txt` | 150–200 words built from your degree, best-matching experience and project, skills, languages, sector motivation and availability window |
| `linkedin_note_en.txt` | ≤ 300-character connection note with a `[Name]` placeholder for you to fill in |
| `job.md` | The posting, its score, the link to apply **manually**, and a pre-send checklist |
| `warnings.txt` | Missing translations, length issues, rejected LLM edits… (only if any) |

#### Optional LLM polishing

```bash
pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...
jobhunt prepare 12 --llm anthropic     # model: claude-opus-5-5 (override with JOBHUNT_LLM_MODEL)
```

The LLM only *rephrases* the template output. Its result is discarded (and you are told why)
if it adds a number, name or URL that is not in your profile or the posting, breaks the
150–200 word / 300 character limits, or the call fails. The default `--llm none` mode is
complete on its own.

### 5. The daily routine: `jobhunt run`

```bash
jobhunt run                                   # discover + score + show NEW matches
jobhunt run --prepare                         # ...and generate CV/letter/note for each
jobhunt run --export shortlist.md             # ...and save the shortlist as markdown
jobhunt run --notify telegram --prepare       # ...and send them to your Telegram
```

`run` shows only offers **first seen since the previous run**, with score ≥
`scoring.min_score_to_prepare`, that pass `preferences.locations.run_filter` (by default: no
offers that need a visa you lack; tiers preferred/acceptable/other). Jobs that were already in
the tracker before your first `run` are never reported. The shortlist markdown has company,
title, location, score, reason, link and a description excerpt.

With `--prepare`, nothing is generated if `profile.yaml` still has `TODO` fields: you get a
warning and the rest of the run (listing, export, notifications) still happens.

### 6. Track your pipeline

```bash
jobhunt status                          # counts per state + table
jobhunt status 12                       # detail + history
jobhunt status 12 applied --note "via careers site"   # after YOU applied (Spanish: aplicado)
jobhunt followups                       # applications with a follow-up due (7 days later)
jobhunt status 12 follow-up             # after you sent a follow-up -> next reminder in 7 days
jobhunt status 12 interview             # or: rejected / entrevista / rechazado
jobhunt export output/applications.csv  # open in Excel / Google Sheets
```

Global options (or env vars): `--profile` (`JOBHUNT_PROFILE`), `--db` (`JOBHUNT_DB`),
`--companies` (`JOBHUNT_COMPANIES`), `--out` (`JOBHUNT_OUTPUT`).

## Telegram notifications

`jobhunt run --notify telegram` sends **one short message per new match** (company, title,
location, score, reason, link) to a chat with **your own bot**; with `--prepare` it also
attaches the CV and the cover letter. Nothing is sent to anyone else.

1. **Create the bot.** In Telegram, open a chat with **@BotFather** (check the blue tick),
   send `/newbot`, choose a display name and a username ending in `bot`. BotFather replies
   with a **token** like `123456789:AA...`. Treat it like a password.
2. **Find your chat id.** Open a chat with your new bot and send it any message (e.g. `hola`).
   Then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser (replace `<TOKEN>`)
   and look for `"chat":{"id":123456789,...}`. That number is your **chat id**.
3. **Store both as environment variables** — never in the repository or in `profile.yaml`:
   - Windows (PowerShell, current user; open a **new** terminal afterwards):
     ```powershell
     setx TELEGRAM_BOT_TOKEN "123456789:AA..."
     setx TELEGRAM_CHAT_ID "123456789"
     ```
     or *Start → "Edit environment variables for your account"* (*"Editar las variables de
     entorno de esta cuenta"*) → **New…** for each variable.
   - macOS / Linux: `export TELEGRAM_BOT_TOKEN=...` and `export TELEGRAM_CHAT_ID=...` in your
     shell profile.
4. **Test it:** `jobhunt run --notify telegram`. Without the variables the command still
   works and prints that no notification was sent.

Privacy note: with `--prepare` your CV and letter (personal data) are uploaded to Telegram's
servers, to your private chat with your bot. Use `-NoPrepare` in the daily script (or drop
`--prepare`) if you prefer to receive only the links.

## Daily automation on Windows (Task Scheduler)

`scripts\run_daily.ps1` activates `.venv`, reads the Telegram variables from your user
environment, runs `jobhunt run --notify telegram --prepare` (plus a dated shortlist in
`output\shortlist_<date>.md`) and writes a log to `logs\run_<date>.log` (kept 30 days).

First, check it by hand from the repository folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_daily.ps1
Get-Content (Get-ChildItem .\logs\run_*.log | Sort-Object LastWriteTime | Select-Object -Last 1)
```

Then schedule it (Spanish Windows labels in parentheses):

1. Open **Task Scheduler** (*Programador de tareas*): Start → type `taskschd.msc`.
2. **Action → Create Task…** (*Acción → Crear tarea…*; not the "basic task" wizard, which
   hides the setting in step 6).
3. **General** tab (*General*): name `job-hunt-copilot daily`; select **Run only when user is
   logged on** (*Ejecutar solo cuando el usuario haya iniciado sesión*), so the task sees your
   user environment variables and files.
4. **Triggers** tab (*Desencadenadores*) → **New…** (*Nueva…*): **Daily** (*Diariamente*), e.g.
   at 08:30, recur every 1 day → OK.
5. **Actions** tab (*Acciones*) → **New…** (*Nueva…*): **Start a program** (*Iniciar un
   programa*)
   - Program/script (*Programa o script*): `powershell.exe`
   - Add arguments (*Agregar argumentos*):
     `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\job-hunt-copilot\scripts\run_daily.ps1"`
   - Start in (*Iniciar en*): `C:\path\to\job-hunt-copilot`
6. **Settings** tab (*Configuración*): tick **"Run task as soon as possible after a scheduled
   start is missed"** (*"Ejecutar la tarea lo antes posible si se pasó una hora de inicio
   programada"*), so a day when the PC was off or asleep at 08:30 runs as soon as you log in.
   Optionally tick "If the task fails, restart every 10 minutes, up to 3 times".
7. **Conditions** tab (*Condiciones*): on a laptop, untick "Start the task only if the computer
   is on AC power" if you want it to run on battery.
8. OK, then right-click the task → **Run** (*Ejecutar*) to test it, and check **Last Run Result**
   (*Resultado de la última ejecución*: `0x0` = success) and the newest file in `logs\`.

Equivalent PowerShell (run once, as your user):

```powershell
$repo = "C:\path\to\job-hunt-copilot"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $repo `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\run_daily.ps1`""
$trigger = New-ScheduledTaskTrigger -Daily -At 8:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable   # = "run as soon as possible after a missed start"
Register-ScheduledTask -TaskName "job-hunt-copilot daily" -Action $action -Trigger $trigger -Settings $settings
```

## Project layout

```
companies.yaml               employers read on every run (ATS + slug + sector)
companies.candidates.yaml    employers to verify with check-boards before adding
profile.template.yaml        commented template -> copy to profile.yaml (git-ignored)
profile.example.yaml         fictional filled-in example (used by tests and the demo)
scripts/run_daily.ps1        Windows Task Scheduler entry point
src/jobhunt/
  profile.py                 load/validate profile, TODO detection, EN/ES text, fingerprint
  models.py                  Company and normalized Job
  sources/                   base.py (interface, fetchers), greenhouse/lever/ashby, check.py
  location.py                place parsing, visa checks, location tiers
  scoring.py                 0-100 score, explanation, date windows, dedup
  pipeline.py                re-scoring on profile change, location filters
  tracker.py                 SQLite states, history, follow-ups, run markers, CSV
  generator/                 tailoring, letters (Jinja2), cv_pdf (reportlab), llm (optional)
  shortlist.py, notify.py    markdown shortlist, Telegram
  cli.py                     Typer commands
tests/                       pytest suite + JSON fixtures in tests/fixtures/
```

## Development

```bash
pytest                 # connectors (fixtures + local HTTP server), location, scoring, tracker, generator, run/Telegram (fake HTTP), CLI
ruff check src tests && ruff format --check src tests
```

CI (GitHub Actions) runs lint, the test suite on Python 3.12 and 3.13, and the offline
end-to-end demo.

## Limitations and next steps

- Location parsing uses a built-in list of cities, countries and regions; an unknown city is
  ranked `unknown` (not `visa`). Run `jobhunt locations` to see how your real data is
  classified and report/extend anything misread.
- Visa logic is country-level: it cannot know about special programmes or sponsorship offered
  in a posting. Read the posting before discarding something you really want.
- Heuristics (internship detection, date parsing, language detection) are keyword-based and
  will miss unusual phrasings. The score is a triage aid, not a verdict: read the postings.
- Skill names are printed as written in your profile (they are not translated).

## Ethics

This tool exists to save you time on the boring parts, not to mass-apply. Recruiters can tell
when applications are spammed, and many platforms (including LinkedIn) prohibit automation.
Apply selectively, tailor what the copilot drafts, and only send things that are true.
