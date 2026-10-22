# 🔄 XLBD Translator
> English translation of README.md. The Chinese README is the source of truth; if they differ, follow README.md.

**An AI translation engine for very large documents.**

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

**XLBD Translator** uses a modern state-driven architecture to translate complex EPUB and PDF documents (including scanned ones) into fluent, accurate Chinese through highly customizable "expert modes" (Personas), and finally produces styled PDF and Markdown files.

## ✨ Key Features

### 🎯 Industrial-Grade Stability
- **State-driven architecture**: the whole translation pipeline is driven by in-memory data structures and persisted to files.
- **Resumable runs**: recovers cleanly after an unexpected interruption, automatically skipping segments that are already translated, so you never start over.
- **Atomic saves**: progress is saved immediately after each batch is translated, minimizing the risk of data loss.
- **Structured output**: Gemini uses `response_schema`, and DeepSeek/OpenAI-compatible providers use `response_format=json_object`, to guarantee valid JSON at the source; local Ollama keeps a lightweight regex safety net.
- **Post-translation QC loop**: after translation, the pipeline scans for failure markers and glossary violations and re-translates the affected segments in a targeted way (at most 1 round). Remaining failures are reported explicitly in `quality_report.json`, so `[Failed]` is no longer silently rendered into the final output (can be turned off with `PROCESSING__ENABLE_QUALITY_CHECK=false`).
- **Async concurrency**: multiple batches are translated concurrently, with results kept in exact order of the source text to avoid misalignment.

### 🤖 Multimodal Translation
- **Text mode (Native)**: for PDF/EPUB files with clean text, extracts and translates directly; fast.
- **Vision mode**: for scanned or complex-layout documents, pages are rendered to images and translated by a multimodal model.
- **Smart strategy**: in the interactive session you can choose auto-detect, force on, or force off for vision mode.

### 🌐 Multi-API Support
- **Google Gemini**: the default translation engine; supports multimodal input and long context
- **DeepSeek API**: supports 128K context, cost-effective, with particular optimization for Chinese translation
  - Automatically detects and enables long-context mode
  - The full system + instruction + mode + context is merged into a single user message
  - See the DeepSeek configuration example in "3.2 Edit the configuration file" below
- **Ollama (local)**: supports locally deployed models, suited to offline or privacy-sensitive scenarios
- **OpenAI-compatible**: supports any OpenAI-compatible API

### 🎭 Professional Translation Personas
- **Highly customizable**: edit `config/modes.json` to modify existing expert roles or create new ones.
- **Built-in expert modes** (in sync with `config/modes.json`; select with `--mode <ID>`):

    | ID | Mode | Use case |
    |----|------|----------|
    | 1 | Continental Philosophy | Hegel/Heidegger/Sartre/Lacan/Derrida/Deleuze, with terminology fixed by school |
    | 2 | Biography & History | Biographical and historical non-fiction; consistent proper names + historical register |
    | 3 | Critical Theory | Critique of ideology / film theory / Frankfurt School / Žižek, including translator's-note conventions |
    | 4 | AI & Data Science | AI papers and technical docs; modern LLM-era vocabulary + fidelity for symbols and code |
    | 5 | Novel & Fiction | Fiction translation; nuanced emotion + consistent character register |
    | 6 | Nietzsche Specialist | Zarathustra style; metaphor deconstruction + rhythmic prose |
    | 7 | Digest Mode | Condensed summaries that make a book thinner |
    | 8 | Logic Analyst | Makes implicit logic explicit in square brackets (default) |

### ⚙️ Highly Configurable
- **`.env`-driven**: all core settings are managed through the `config/config.env` file, enabling non-interactive runs.
- **Custom table of contents (TOC)**: inject a chapter structure into a PDF via an external CSV file for more precise semantic splitting.
- **Custom PDF styling**: edit `config/pdf_style.css` to fully control the fonts, margins, colors, and other aspects of the final PDF.

## 🚀 Quick Start

### 1. Prepare the Environment

- Clone this repository:
  ```bash
  git clone https://github.com/xiaolibird/XLBDTranslator.git
  cd XLBDTranslator
  ```
- We recommend creating and activating a Python virtual environment with `conda`:
  ```bash
  conda create -n xlbd-translator python=3.12
  conda activate xlbd-translator
  ```

### 2. Install Dependencies

- Install all required Python libraries:
  ```bash
  pip install -r requirements.txt
  ```

- **PDF output support** (recommended):

  By default the project outputs both **Markdown (.md)** and **PDF (.pdf)**. PDF generation depends on the `weasyprint` library.

  - **macOS users**: install extra system dependencies
    ```bash
    brew install cairo pango gdk-pixbuf
    ```

  - **Ubuntu/Debian users**:
    ```bash
    sudo apt-get install libpango1.0-dev libcairo2-dev libgdk-pixbuf2.0-dev
    ```

  - **Windows users**: the GTK3 runtime is recommended
    1. Download and install the [GTK3 Runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases)
    2. Or install the dependencies with conda:
       ```bash
       conda install -c conda-forge weasyprint
       ```

  - If `weasyprint` fails to install, the program automatically skips PDF generation and can still produce Markdown files normally.

### 3. Configuration

#### 3.1 Create the configuration file

Core settings are managed through an environment variable file. The project provides a template, `config/config.env.template`; create the actual configuration file before first use:

```bash
# Create the config file from the template
cp config/config.env.template config/config.env
```

**Note**: `config/config.env` contains sensitive information (such as API keys). It is excluded by `.gitignore` and will not be committed to the repository.

#### 3.2 Edit the configuration file

Open `config/config.env` and edit it as described below:

1.  **Choose the translation engine**:
    ```dotenv
    # Options: gemini (default), openai-compatible (supports DeepSeek/OpenAI/Ollama)
    API__TRANSLATOR_PROVIDER=gemini
    ```

2.  **API key (required)**:

    **Using Gemini**:
    ```dotenv
    # API key from Google AI Studio
    API__GEMINI_API_KEY="YOUR_API_KEY_HERE"
    ```

    **Using DeepSeek** (recommended for Chinese translation):
    ```dotenv
    # DeepSeek API (OpenAI-compatible)
    API__TRANSLATOR_PROVIDER=openai-compatible
    API__OPENAI_API_KEY="sk-your-deepseek-api-key"
    API__OPENAI_BASE_URL="https://api.deepseek.com"
    API__OPENAI_MODEL="deepseek-v4-flash"
    ```

    **Automatic multi-provider fallback** (optional):
    ```dotenv
    # When the primary provider runs out of balance (402), fails authentication (401/403),
    # has its model retired (404), or keeps exhausting its quota (429), the pipeline
    # switches automatically to the next provider in the fallback chain, with no manual
    # intervention; resumable runs are unaffected.
    # gemini and deepseek are parallel primary options (switch by changing TRANSLATOR_PROVIDER).
    API__TRANSLATOR_PROVIDER=deepseek
    API__FALLBACK_PROVIDERS=gemini
    ```
    There is also an experimental `claude-agent` provider (the local claude CLI in
    headless mode, billed through a subscription, no API key needed). However, the
    Claude API's copyright output filter will probabilistically block full-passage
    translation of copyrighted books, so it is unreliable for book translation and
    is not recommended for the default fallback chain.

3.  **Document path (required)**:
    ```dotenv
    # Full path of the EPUB or PDF file to translate
    FILES__DOCUMENT_PATH="/path/to/your/document.pdf"
    ```

3.  **Model selection (optional)**:
    - Run `python scripts/check_models.py` to see the currently available models (the README does not maintain a hard-coded list; the script output is authoritative).
    ```dotenv
    # Primary: DeepSeek (currently in use)
    API__OPENAI_MODEL="deepseek-v4-flash"
    # Fallback: Gemini (currently in use)
    API__GEMINI_MODEL="gemini-3.5-flash"
    ```

4.  **Other common settings (optional, for non-interactive runs)**:
    - If these are set in the `.env` file, the program uses the values directly and skips the corresponding interactive prompts.
    ```dotenv
    # --- Document-specific strategy ---
    # Path to a custom TOC file
    DOCUMENT__CUSTOM_TOC_PATH="./test/my_toc.csv"
    # Page range (e.g. "10-50" or "[10, 50]")
    DOCUMENT__PAGE_RANGE="10,50"

    # --- Processing strategy ---
    # Whether to keep the original text for a bilingual side-by-side output (true/false)
    PROCESSING__RETAIN_ORIGINAL=true
    # Whether to enable vision mode by default (true/false; unset means auto)
    PROCESSING__USE_VISION_MODE=true
    ```

### 4. Run a Translation

#### 4.1 Basic usage

Once everything is set up, run the main program:

```bash
# Use the default config file (config/config.env)
python main.py

# Specify the document to translate
python main.py /path/to/document.pdf

# Use a custom config file
python main.py --config /path/to/custom.env document.epub
```

#### 4.2 Command-line arguments

The program supports full control of the translation pipeline through command-line arguments, enabling non-interactive runs. Precedence is: **command-line arguments > config file > interactive prompts**.

##### Basic arguments

| Argument | Description | Example |
|------|------|------|
| `file_path` | Path of the document to translate (positional argument) | `python main.py document.pdf` |
| `--config` | Path of the config file | `--config config/custom.env` |

##### Translation mode arguments

| Argument | Description | Example |
|------|------|------|
| `--mode` | Translation mode ID (corresponds to a key in modes.json) | `--mode 3` |
| `--provider` | Translation provider (overrides config.env): `deepseek`/`gemini`/`claude-agent`, etc. | `--provider claude-agent` |
| `--preset` | Config preset: `fast`/`quality`/`balanced`/`debug`/`economy`/`bilingual` | `--preset quality` |

##### PDF-specific arguments

| Argument | Description | Values/format | Example |
|------|------|-------------|------|
| `--vision-mode` | Vision mode control | `auto` (auto-detect)<br>`force` (force on)<br>`off` (text only) | `--vision-mode force` |
| `--page-range` | Page range | `"start,end"` or `"start-end"` | `--page-range 10-50` |
| `--margins` | Crop margins (removes headers and footers) | `"top,bottom,left,right"` (ratios from 0.0 to 1.0) | `--margins 0.1,0.05,0.05,0.05` |

##### General arguments

| Argument | Description | Example |
|------|------|------|
| `--retain-original` | Keep the original text in the output (bilingual) | `--retain-original` |
| `--no-retain-original` | Do not keep the original text in the output | `--no-retain-original` |

##### Batch translation (multiple books / a whole directory)

```bash
# Recursively collect .epub/.pdf from a directory and translate them in sequence; each book has its own log and checkpoint, and one failure does not interrupt the queue
python scripts/translate_books.py ~/Downloads/wanna-read --mode 3 --retain-original

# Preview the list without executing
python scripts/translate_books.py ~/Downloads/wanna-read --dry-run
```

#### 4.3 Usage examples

##### Fully non-interactive run

All arguments are given on the command line, suitable for scripting and batch jobs:

```bash
# Translate a PDF, specifying all arguments
python main.py document.pdf \
  --mode 1 \
  --vision-mode force \
  --page-range 10-50 \
  --margins 0.1,0.05,0.05,0.05 \
  --retain-original

# Translate an EPUB, specifying only the mode and keeping the original
python main.py novel.epub \
  --mode 5 \
  --retain-original

# Translate with the DeepSeek API (set API__TRANSLATOR_PROVIDER and the other DeepSeek parameters in config.env, see Section 3.2)
python main.py document.pdf \
  --config config/config.env \
  --mode 4 \
  --page-range 1-100
```

##### Mixed mode

Some arguments come from the command line, the rest from the config file or interactive prompts:

```bash
# Specify only the mode and page range; read the other arguments from the config file
python main.py document.pdf --mode 2 --page-range 1-100

# Specify only the document and mode; the other arguments are asked interactively
python main.py document.pdf --mode 1
```

##### Docker/CI environments

In containers or continuous-integration environments, combine with a config file for zero interaction:

```bash
# Everything is preset in config.env
python main.py

# Or use environment variables and command-line arguments
export API__GEMINI_API_KEY="your-api-key"
python main.py /data/document.pdf \
  --mode 1 \
  --vision-mode off \
  --no-retain-original
```

#### 4.4 Interactive configuration

When an argument is specified neither on the command line nor in the config file, the program guides you through configuration with an interactive command-line session:

1.  **Choose a translation mode (Persona)**: pick the expert identity that best fits your text type.
2.  **Configure the processing strategy**:
    - **Custom TOC**: (PDF only) asks whether to load an external CSV TOC file.
        - **Format requirement**: the CSV file must contain the three headers `Page`, `Title`, and `Level`.
    - **Vision mode**: (PDF only) choose auto-detect, force on, or force off.
    - **Page range**: (PDF only) specify the start and end pages to translate.
    - **Margin cropping**: (vision mode only) set crop ratios to remove headers and footers.
    - **Keep original**: choose whether to produce a bilingual output.

After configuration, the translation starts automatically, and you can follow real-time progress logs in the terminal.

## 🎓 Scholar Digest (Google Scholar Email Digest)

Besides document translation, the project includes a standalone Scholar Digest module: it reads Google Scholar paper-alert emails from Gmail (optionally merging in PubMed/arXiv searches), filters them in two stages, and then calls an LLM to generate a Chinese paper-summary digest.

**Three-state methodological review verdicts**: the blacklist stays a zero-cost deterministic keyword match that removes only clearly off-topic fields without ambiguity (conservative, to avoid wrongly dropping adversarial evidence). Relevance judgment is handed by default to an LLM doing a methodological review (`PROCESSING__FILTER_MODE=llm`, switchable back to `keyword`), which outputs a three-state `INCLUDE / MAYBE / EXCLUDE` verdict plus the inclusion dimension (bucket), danger signals (`THREAT`/`BENCHMARK`/`OVERCLAIM_PRECEDENT`), a role (`MUST_ENGAGE`, etc.), and a one-line statement of usefulness. `MAYBE` and `INCLUDE` papers both go into translation and summarization, with verdict badges distinguishing them in the output; papers tagged `THREAT`/`MUST_ENGAGE` are pinned to the top of the priority ordering. For every verdict, the decision, rationale, model, and prompt version, together with the full metadata of excluded papers, are persisted to `{run_id}_excluded.json` for auditing and traceability; if the LLM call fails, the pipeline falls back to keyword matching without interrupting the run. The review dimensions (inclusion dimensions A-G, exclusion dimensions X1-X7) can be customized in `config/research_profile.yaml`, and the paper topic is injected through `PROCESSING__RESEARCH_INTERESTS`.

**PubMed / arXiv search sources**: besides Gmail, the module can fetch PubMed (E-utilities) and arXiv (Atom API) by search query, deduplicate against the email results, and merge them into the same filter → translate → summarize pipeline. This runs automatically with the weekly digest by default (`PROCESSING__EXTERNAL_SOURCES_ENABLED=true`, overridable with `--external`/`--no-external`). All interfaces are public and need no key; network requests are skipped automatically under `--dry-run`. See `PROCESSING__ARXIV_QUERY` / `PROCESSING__PUBMED_QUERY` for the search queries.

**Zotero integration + pandoc research notes**: selected papers can be written to Zotero in one step (local connector `saveItems`), with [Better BibTeX](https://retorque.re/zotero-better-bibtex/) assigning citekeys automatically. A pandoc-ready note is then generated: one markdown file per paper, containing YAML front matter, `[@citekey]` citations in the body, the three-state verdict badge, the abstract, and an AI-written synthesis, plus a self-contained `references.json` (CSL-JSON). Together with the `references.bib/json` that Zotero auto-exports, you can run `pandoc --citeproc` directly to produce LaTeX/docx. OA full text (direct arXiv link / Unpaywall, legal and free) is handed to Zotero as an attachment for it to fetch itself; for non-OA papers it degrades to abstract-only. It is off by default (writing to the library needs the Zotero desktop app online). The recommended flow separates the steps: "cron produces the digest → when you are at the machine, push with `python scholar_main.py zotero --input <run_id>.json`", or add `--zotero` when running the digest. See `PROCESSING__ZOTERO_*` / `PROCESSING__NOTES_*` for configuration.

### Configuration

```bash
# 1. Create the config file from the template (contains keys; excluded by .gitignore, never committed)
cp config/scholar.env.template config/scholar.env

# 2. Edit config/scholar.env and fill in the Gemini API key and the Gmail OAuth credentials path
#    Note: nested settings use double-underscore names, e.g. LLM__GEMINI_API_KEY, GMAIL__CREDENTIALS_PATH

# 3. Install the Scholar-specific dependencies
pip install -r requirements_scholar.txt
```

Download the Gmail OAuth credentials (`config/credentials.json`) from the [Google Cloud Console](https://console.cloud.google.com/). On first run you are guided through authorization, which generates `config/token.json` (both files are excluded by .gitignore).

### Run

```bash
# Generate a paper-summary digest (read recent emails -> filter -> LLM summary -> output to output/scholar_digest/)
python scholar_main.py digest

# Generate summaries with DeepSeek v4 (reuses the DeepSeek key in config/config.env)
python scholar_main.py digest --provider deepseek

# Backfill historical emails (e.g. the last 151 days); the Markdown output is split by month automatically when it spans months
python scholar_main.py digest --days 151 --max-emails 0 --provider deepseek

# Use the traditional keyword whitelist (skip the LLM verdict)
python scholar_main.py digest --filter-mode keyword

# Merge in PubMed/arXiv search sources (on by default; can be overridden explicitly)
python scholar_main.py digest --external
python scholar_main.py digest --no-external   # Gmail only

# Zotero integration: write an existing digest into Zotero + generate pandoc notes (Zotero must be open)
python scholar_main.py zotero --input output/scholar_digest/digest_xxx.json
# Or push together with the digest (when you are at the machine)
python scholar_main.py digest --provider deepseek --zotero

# Deep research mode
python scholar_main.py deep-research --papers output/scholar_digest/digest_xxx.json
```

> Batch paper translation has been merged into the main `scholar_main.py` pipeline; a separate script is no longer needed.

### Full-Text Close Reading (sentence-level role tags + retrievable highlights)

For the top-N (default 5) priority papers each month, the pipeline performs **full-text** close reading: resolve the OA full text (direct arXiv link / Unpaywall, legal and free) → download the PDF → extract text with PyMuPDF → a strong model (`LLM__CLOSEREAD_MODEL`) produces a structured Chinese close reading (research question / methods and data / key findings / points to challenge / **connections to my research**), and tags sentences with roles by **their use in downstream workflows**: `〔可引用证据〕` dark green (contains numbers/effect sizes, for evidence in writing) / `〔可反驳观点〕` purple (the author's claims / points open to challenge, targets for writing a critique) / `〔方法论借鉴〕` blue (transferable methodological ideas) (the docx version uses true three-color shading). These sentences are aggregated into the index entry's `highlights[]`, so workflows can pull them across the whole library by role (citable/refutable/method) with `jq`, without opening the md. Historical notes keep the old three-color tags (methodological innovation / important finding / background); the index layer approximates them to the new roles automatically. The candidate layer does **OA preference**: among high-priority candidates it prefers those with obtainable full text, avoiding a top-N that happens to be all paywalled; papers without full text degrade to abstract-level close reading, clearly marked as such. Toggle with `PROCESSING__CLOSEREAD_ENABLED` or the CLI flag `--close-read`.

### Monthly Research-Note Backfill (headless)

```bash
# Backfill a single month / a range: Gmail+PubMed+arXiv → three-state screening → authoritative metadata correction → top-5 full-text close reading
#   → output/scholar_notes/科研札记_YYYY-MM_全文精读.{md,docx,references.json,index.json}
python scripts/backfill_notes.py --since 2025-06 --until 2025-06
python scripts/backfill_notes.py --prev-month          # previous calendar month (used by the monthly launchd job)
```

Headless design: it does not write to the Zotero library (metadata is corrected by translation-server + Crossref; citekeys use a human-readable fallback key of "author surname + year + title word", and when you need authoritative keys, push to the library later with `zotero --input` while at the machine). Months that already exist are skipped automatically (`--force` overwrites); **cross-run deduplication** — the `seen` set is restored from the literature index, so a new month never re-includes a paper from an earlier month. When running history in parallel shards, give each process its own `--token-path` to prevent concurrent Gmail token refreshes from corrupting the token.

### Manual PDF Deep Reading (agent reads it personally + script cross-check)

Use this when you have a PDF in hand and want a **more thorough read than the automatic top-N** and archive it into the literature library. Three steps:

```bash
# 1) ingest: extract full text (no truncation) + fetch authoritative Crossref/arXiv metadata + read chunk by chunk → draft bundle
PYTHONPATH=. python scripts/read_pdf.py ingest paper.pdf            # archives into the current month by default; use --month YYYY-MM to change
# 2) agent reads it personally: use the Read tool to read the whole PDF, verify the script draft's numbers/conclusions/methods one by one,
#    write the merged final text back into the bundle's close_reading_final + cross_check_report, with status=final (protocol: see skill: read-paper)
# 3) finalize: rebuild the manual-close-reading four-file set from all final bundles of the month + refresh the index (multiple papers can be appended in the same month; idempotent)
PYTHONPATH=. python scripts/read_pdf.py finalize <bundle.json>
```

The output is a separate series, `科研札记_YYYY-MM_手动精读.{md,docx,references.json,index.json}`, which is **not merged into** the automatic `_全文精读` (otherwise the monthly launchd job would see "this month already exists" and skip the whole month). In the index, the manual deep read is the keeper — when the paper-writing agent searches (filtering on `duplicate_of == null`), what it reads is the most thorough version of the close reading. The script's deep read and Claude's personal read are **cross-checked**: every effect size/number is checked against the PDF original, disagreements are resolved in favor of what was verified firsthand, and corrections and omissions are recorded in the closing section of the note, "Cross-check record". Trigger skill: in this repository, tell Claude "close-read this PDF / deep-read this paper" and provide the PDF.

### Literature Index (the retrieval entry point for the paper-writing agent)

The notes directory maintains a machine-readable master index, `output/scholar_notes/literature_index.json` (plus a human-readable `INDEX.md` and `AGENTS.md`, usage notes for agents), with one entry per paper: citekey/DOI/arXiv id, title, verdict, one-line usefulness, priority, whether it was close-read in full text, role-tag counts (`tag_counts`: citable/refutable/method), **sentence-level retrievable `highlights[]`** ({role,tag,section,text}), the note file + line number where it lives, cross-month duplicate marker (`duplicate_of`), and **citekey collision warnings** (`citekey_collisions`, which must be checked before merging bibliographies).

```bash
python scripts/notes_index.py                       # incremental (also refreshed automatically when notes are written)
python scripts/notes_index.py --full                # full rebuild
python scripts/notes_index.py --since 2025-01 --until 2025-12   # force a rescan of a range
```

Data sources: new notes get a lossless sidecar `{note name}.index.json` written automatically by `write_notes`; existing notes are parsed according to the md format contract (`test/test_notes_index.py::test_roundtrip_md_parse` locks the format). **Agent consumption**: the global skill `~/.claude/skills/scholar-notes/` (source in `docs/skills/scholar-notes/`; saying "find literature / look up notes" in any project triggers it), or read `AGENTS.md` in the notes directory directly (source `docs/agents/scholar_notes_AGENTS.md`, deployed automatically by the index script). The four-step retrieval method: query the index (filter `duplicate_of == null`) → `grep '[@citekey]'` to locate the close-reading section → cite with `[@citekey]` in the body → merge the corresponding months' `references.json` to produce the bibliography.

### Automatic Runs (launchd)

Scheduled-job templates are in `config/launchd/com.xlbd.scholar-*.plist` and are installed by `scripts/install_*.sh` (add `--uninstall` to each script to uninstall; manual trigger: `launchctl kickstart gui/$(id -u)/<label>`):

| label | Trigger | What it does |
|---|---|---|
| scholar-digest | Every Monday 09:00 | `scholar_main.py` produces a digest overview (no notes) |
| scholar-weekly-ingest | Every Monday 09:30 | `ingest_notes.py` reuses the digest verdicts to produce weekly notes |
| scholar-monthly | 1st of each month 21:30 | `backfill_notes.py` produces last month's close-reading notes and refreshes the literature index |
| scholar-drift | 1st of each month 10:30 | `profile_drift.py` research-profile drift alert |
| scholar-confirm | Every day 10:00 | `confirm_ingest.py` ingest confirmation |
| scholar-inventory | Every day 09:10 | `inventory_missing_ids.py` resumes the no-DOI inventory |
| scholar-backup | Every Sunday 20:00 | `backup_snapshot.py` notes-library snapshot (see `docs/guides/backup_restore.md` for restore) |
| scholar-embed | When notes change | `notes_embed.py` incremental vector-store sync |
| scholar-vault | When notes change | `sync_vault.py` syncs the Obsidian vault |

The Gmail tokens of digest and monthly are physically isolated (monthly uses a copy, `config/token.monthly.json`), so they do not conflict; all jobs reuse the same python binary, so Full Disk Access under TCC needs to be granted only once. Logs are in `~/Library/Logs/xlbd-scholar-digest/cron_*.log`; if a trigger time is missed (sleep/power-off), the job catches up automatically on wake.

> ⚠️ When the repository lives in a macOS privacy-protected directory such as `~/Documents`, a one-time authorization is required: System Settings > Privacy & Security > **Full Disk Access**, then add the python binary path printed by the install script; otherwise background jobs fail with `Operation not permitted`.

## 🧩 Agent Skills (Claude Code / Codex)

The repository ships with 11 skills; the source of truth is `docs/skills/<name>/SKILL.md`. After any edit you must run the install script once:

```bash
bash scripts/install_skills.sh            # sync to ~/.claude/skills/ and maintain the .agents/skills/ symlinks (the Codex entry point)
bash scripts/install_skills.sh --dry-run  # only show what would be synced
```

| skill | Purpose |
|---|---|
| read-paper | Deep close reading of a single PDF: script draft + agent's personal read with cross-check, archived into the notes library |
| read-book | Whole-book reading: split by TOC into chapters, triage, deep-read only relevant chapters, with page-numbered quote verification |
| scholar-notes | Browse/search notes-library literature and retrieve evidence by role (citable evidence / refutable / method borrowing) |
| scholar-search | Ad-hoc arXiv/PubMed search, not ingested, marking whether a hit is already in the library |
| scholar-write | Evidence retrieval for writing: write `[@citekey]` in the body, pandoc attaches the global bibliography to produce docx/LaTeX |
| ask-notes | Ask the notes library a question and archive the answer with sentence-level provenance to `topics/qa/` |
| screen-journal | PubMed search + three-state verdicts over a whole journal, producing a candidate list |
| paper-audit | Forensic integrity audit: numeric self-consistency, text-figure cross-checks, citation back-checks, retraction status |
| manuscript-selfcheck | Adversarial self-check with 15 hard gates before submission |
| translate-paper | Verbatim Chinese translation of an arXiv paper, typeset side by side as a PDF (beta) |
| translate-book | Whole EPUB/PDF translation: choose a mode, review terminology, produce PDF/EPUB/MD reading editions |

See [`docs/skills/OUTPUTS.md`](docs/skills/OUTPUTS.md) for where each skill puts its outputs.

## 📚 Documentation Map

| Location | Contents |
|---|---|
| `docs/skills/` | Each skill's SKILL.md, companion scripts, and templates; `OUTPUTS.md` is the master table of output locations |
| `docs/agents/` | Instructions deployed for agents to read: `scholar_notes_AGENTS.md` (deployed to the notes directory by `notes_index.py`), `paper_agent_prompt.md` |
| `docs/guides/` | Operations manuals: `backup_restore.md` (backup and restore), `closeread_deep_rollout.md` (rollout record for the close-reading deepening) |
| `docs/decisions/` | Design decision records (by date) |
| `docs/bugs/` | Bug ledger; see `docs/bugs/README.md` for the index |

## 📁 Project Structure

```
XLBDTranslator-dev/
├── main.py                  # Book translation entry point
├── scholar_main.py          # Scholar entry point (email/search digest, monthly backfill, notes)
├── requirements*.txt        # Dependencies: translation / Scholar / development
├── config/                  # Configuration
│   ├── *.env.template       # Config templates (the real config.env / scholar.env are not committed)
│   ├── modes.json           # Translation personas (Persona)
│   ├── pdf_style.css        # PDF styling
│   ├── prompts/             # Various prompts
│   ├── research_profile.yaml, topics.yaml, venue_lists.yaml, dedup_overrides.json  # Notes-library config
│   └── launchd/             # Scheduled-job templates (com.xlbd.scholar-*.plist)
├── src/
│   ├── core/                # Data models (Pydantic) and exceptions
│   ├── parser/              # PDF/EPUB parsing
│   ├── translator/          # Translation engine and multi-provider fallback chain
│   ├── renderer/            # Markdown / PDF / EPUB rendering
│   ├── workflow/            # Main translation flow and config builder
│   ├── scholar/             # Notes library: digest, close reading, index, vector search, topic pages, Q&A, lint, backup, etc. (about 50 modules)
│   └── utils/               # Utilities for logging, files, UI, etc.
├── scripts/                 # CLI tools and ops scripts (close reading read_pdf.py, search notes_search.py,
│                            #   index notes_index.py, topic pages build_topics.py, the install_*.sh scheduled jobs, etc.)
├── docs/                    # See "Documentation Map" above
├── .agents/skills/          # Codex skill entry points (symlinks to docs/skills/, maintained by install_skills.sh)
├── test/                    # pytest tests and small fixed samples (test/data/, test/fixtures/)
├── output/                  # All run artifacts (not committed, see next section)
└── logs/                    # Run logs (not committed)
```

## 📁 Output Files

All run artifacts live under `output/`, which is not committed as a whole. See [`docs/skills/OUTPUTS.md`](docs/skills/OUTPUTS.md) for the location conventions of each skill.

```
output/
├── translations/            # main.py translation jobs
│   ├── <file hash>/          # Intermediate files for each book (see below)
│   └── _archive/            # Archive of completed jobs (since 2026-08)
├── translations-book/       # translate-book skill: one directory per book (cache/ reviews/ reviewed/ previews/)
├── scholar_notes/           # Notes library (included in the weekly backup)
│   ├── 科研札记_*.md/.docx  # Monthly/batch notes and sidecars
│   ├── literature_index.json, all_references.json, INDEX.md, AGENTS.md
│   ├── manual/  books/      # Manual close-reading bundles, book close readings
│   ├── topics/  topics/qa/  # Topic pages, Q&A archive
│   ├── assets/shots/        # Source-page screenshots
│   ├── deep_research/  audits/  selfchecks/   # Deep research, integrity audits, manuscript self-checks (one dated directory per run)
│   └── _archive/ _eval/ _inventory/ _provenance/ _rekey/   # Archive, evaluation, inventory, provenance, re-keying records
├── scholar_digest/          # Digest overviews
├── journal_screen/          # Journal screening candidates
└── scholar_pdfs/            # Fetched full-text PDFs (re-fetchable, not backed up)
```

**Translation intermediate files** (`output/translations/<file hash>/`):
- `structure_map.json`: the core state, recording each segment's source text, translation, and metadata; resumable runs depend on it
- `checkpoint.json`: recovery checkpoint
- `glossary.json`: the glossary, ensuring consistency throughout
- `quality_report.json`: the post-translation QC report (failed segments / glossary violations) — look here to judge whether a book's translation is usable
- `blocked_segments.json`: record of segments blocked by safety filters (if any)

**Final translation files**: written to `FILES__FINAL_OUTPUT_DIR` (configured as `./output/translations` in this repository; if unset, defaults to the same directory as the source file):
- `_Translated.md`, `_Translated.epub` (an EPUB source gets a bilingual EPUB written back)
- `_Translated.pdf` + `_Translated_mobile.pdf` (desktop and mobile editions; require the WeasyPrint system libraries; on macOS with a conda environment, run with `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`)
- At the end of a run, the log prints a "📦 Artifact list" showing the files actually generated

## 🎨 Advanced Customization

### Modify translation personas (Persona)

Edit `config/modes.json` to tweak existing roles or add new expert modes:

```json
{
  "custom_expert": {
    "name": "自定义专家",
    "role_desc": "您是一位...",
    "style": "您的风格指南...",
    "context_len": "medium"
  }
}
```

### Custom PDF styling

Edit `config/pdf_style.css` to change fonts, font sizes, page margins, colors, and more:

```css
body {
  font-family: '您偏好的字体';
  font-size: 12pt;
  line-height: 1.8;
}
```

## 🤝 Contributing

Contributions are welcome! Feel free to submit a Pull Request.

## 📜 License

This project is released under the [MIT License](LICENSE).

---

**Note**: this tool requires a valid Google Gemini API key. Depending on your API plan, usage may incur costs.
