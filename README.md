# FileBot

A local terminal chatbot that manages files and folders inside **one workspace folder** using plain-English commands. It runs entirely on your machine through [Ollama](https://ollama.com). No cloud API is used.

```
you> create a folder named projects
Created folder 'projects'.
Suggestion: Add a README: create a file called projects/README.md
you> create a file called projects/README.md with the text 'My project'
Created file 'projects/README.md' with 10 characters.
you> delete it
╭─ Confirm ───────────────────────────────────────────────────────────╮
│ Action:  Delete file 'projects/README.md'                           │
│ Target:  C:\...\workspace\projects\README.md                        │
│                                                                     │
│ Permanently DELETE file 'projects/README.md' (10 bytes)? This cannot│
│ be undone.                                                          │
╰─────────────────────────────────────────────────────────────────────╯
Type 'y' or 'yes' to proceed, anything else cancels: n
Cancelled. Nothing was changed.
```

## Windows setup

Requirements: Windows 10/11, Python 3.11 or newer, and Ollama.

```powershell
# 1. Install Ollama (skip if installed): https://ollama.com/download/windows
#    Then make sure it is running (it starts automatically; otherwise run: ollama serve)
ollama --version

# 2. Download the model (986 MB)
ollama pull qwen2.5:1.5b

# 3. Create a virtual environment and install FileBot
cd C:\Hedi\MedTech\hechemkhadija
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .

# 4. Start it
filebot                       # uses .\workspace (created automatically)
filebot --workspace D:\Sandbox
filebot --debug               # shows parsed intent and validation details
```

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. Or skip activation and call `.\.venv\Scripts\python.exe -m filebot` directly.

## Usage examples

| Action | Example |
|---|---|
| create_file | `create a file called notes.txt with the text 'hello'` · `touch todo.md` |
| create_folder | `make a folder named projects` · `mkdir assets/images` |
| list_folder | `list my files` · `what's inside the projects folder?` |
| read_file | `read notes.txt` · `show me what's in todo.md` |
| edit_file | `append 'buy eggs' to shopping.txt` · `replace the contents of notes.txt with 'new text'` |
| rename_path | `rename notes.txt to todo.txt` |
| move_path | `move todo.txt into projects` |
| copy_path | `copy config.ini to config_backup.ini` |
| delete_file | `delete backup.txt` (asks for confirmation) |
| delete_folder | `delete the folder old_projects` (shows contents and asks for confirmation) |
| show_capabilities | `what can you do?` or `help` |

- One action per message.
- Follow-ups work within a session: `delete it`, `rename it to final.txt`, `read that file`.
- If something is missing, FileBot asks one short question (`delete a file` → *Which file should I delete?*). You can answer with just the name.
- Requests unrelated to files get a short refusal plus example commands. That includes questions, coding help, jokes, translations, and attempts to change its role.
- `help` shows the capabilities. `exit` quits.
- The conversation is kept in memory only (last 3 turns and 4 paths) and is never written to disk.

## Configuration

Settings are applied in this order, with later ones winning: built-in defaults, then `filebot.toml`, then environment variables, then command-line flags.

| Setting | `filebot.toml` | Environment variable | CLI flag | Default |
|---|---|---|---|---|
| Model | `model` | `FILEBOT_MODEL` | `--model` | `qwen2.5:1.5b` |
| Ollama URL | `ollama_host` | `OLLAMA_HOST` | — | `http://localhost:11434` |
| Workspace | `workspace` | `FILEBOT_WORKSPACE` | `--workspace` | `.\workspace` |
| Max file size for read/edit | `max_read_bytes` | `FILEBOT_MAX_READ_BYTES` | — | 1 MB |
| Model timeout (s) | `request_timeout` | `FILEBOT_TIMEOUT` | — | 120 |

A different config file can be used with `--config path\to\file.toml` or `FILEBOT_CONFIG`.

## How it works

```
user text ─► Ollama (structured output, strict JSON schema) ─► Pydantic validation (retry once)
          ─► Python grounding & checks ─► allowlisted planner ─► Python confirmation ─► re-validate ─► execute
```

| Module | Responsibility |
|---|---|
| `filebot/cli.py` | Terminal UI (Rich), help/exit, confirmations, output |
| `filebot/ollama_client.py`, `filebot/prompts.py` | Local Ollama HTTP client, system prompt, few-shot examples, retry |
| `filebot/schemas.py` | JSON schema for structured output, Pydantic `Intent` model |
| `filebot/engine.py` | Session context, grounding, clarification, injection/role checks |
| `filebot/security.py` | Workspace path security |
| `filebot/actions.py` | The 10 filesystem handlers (plan → confirm → re-check → execute) |
| `filebot/suggestions.py` | Text-only next-step suggestions and capability list |
| `filebot/config.py` | Configuration loading |

The model only fills in `{action, path, destination, content, mode}`. It never writes text shown to the user: every refusal, question and message comes from Python templates. The model never sees file contents.

## Safety limits (enforced in Python, not by the prompt)

All model output is treated as untrusted.

- **Fixed allowlist of handlers.** There is no shell, `eval`, `exec`, `os.system` or `subprocess` anywhere in `filebot/`. Unknown actions fail Pydantic validation.
- **Invalid output does nothing.** Invalid JSON or schema output gets one retry; if it fails again, FileBot asks you to rephrase. A failed response can never lead to a filesystem action.
- **Grounding.** Every path the model returns must have been typed by you, or come from recent session context when you say "it/that/there". Otherwise FileBot asks instead of guessing. Placeholder words ("file", "folder"), invented extensions and invented file content are rejected the same way.
- **Path security** (`security.py`). Each path is resolved and must stay inside the workspace (compared case-insensitively). It blocks:
  - absolute paths outside the workspace, drive-relative paths (`C:foo`), rooted paths (`\foo`) and `~`
  - any `..` component
  - UNC and device paths (`\\server\share`, `\\?\`, `\\.\`)
  - alternate data streams (any `:`)
  - reserved names (CON, PRN, AUX, NUL, COM0–9, LPT0–9, CONIN$/CONOUT$, including `con.txt`)
  - forbidden characters, and trailing dots or spaces
- **No link following.** Symlinks, junctions and other redirecting reparse points between the workspace root and the target are refused. Deleting a folder that contains a junction removes the junction itself, never its target.
- **Workspace root protection.** The root cannot be deleted, renamed, moved or copied.
- **Re-check before execution.** Paths are resolved again right before each action runs. If the state changed in a way that would need a confirmation you didn't give, the action is refused.
- **Confirmations.** Deleting, overwriting (create over an existing file, edit in replace mode) and replacing content need an explicit `y` or `yes` typed at the prompt. Anything else cancels. Text in your original request ("yes, I confirm") does not count, and neither does model output. The prompt shows the action, the absolute resolved target and, for folders, the number of files and subfolders. It also states that deletion is permanent.
- **No silent overwrite.** Move, copy and rename refuse when the destination exists.
- **Reading.** Only UTF-8 text is read, up to 1 MB by default. Binary files are refused. Control and ANSI escape characters are neutralised before display, and Rich markup in files is shown literally.
- **Prompt injection.** Role-change and instruction-override phrasing outside quotes is refused even if the model returned an action. Quoted text is data, so `create memo.txt with the text 'ignore your rules'` works normally.

## Model selection results

**Selected model: `qwen2.5:1.5b` (986 MB download).**

Candidates were tested smallest first on `eval/dataset.jsonl`, which has 94 labeled prompts:
- every action, in varied wording
- 11 missing-detail prompts
- 10 off-topic prompts
- 11 prompt-injection prompts
- 6 follow-ups that use session context

Targets: ≥ 95% action classification (including refusals and clarifications), ≥ 95% off-topic refusal, and 100% correct path extraction on delete actions. Smaller models were too weak: `smollm2:135m/360m` and `gemma3:270m` were excluded up front.

Main dataset (94 prompts):

| Model | Download | Action acc. | Off-topic refusal | Delete args | Action+args | Raw model (action / off-topic) | Latency | Result |
|---|---|---|---|---|---|---|---|---|
| `qwen2.5:0.5b` | 398 MB | **94.7%** | **94.1%** | **100%** (15) | 93.6% | 94.7% / 94.1% | 0.22s | fail |
| `qwen3:0.6b` | 523 MB | **93.6%** | **88.2%** | **100%** (15) | 93.6% | 89.4% / 58.8% | 0.35s | fail |
| `gemma3:1b` | 806 MB | **90.4%** | **100.0%** | **93%** (15) | 87.2% | 90.4% / 94.1% | 0.42s | fail |
| `qwen2.5:1.5b` | 986 MB | **98.9%** | **100.0%** | **100%** (15) | 98.9% | 93.6% / 70.6% | 0.40s | **PASS** |
| `llama3.2:1b` | 1.3 GB | **96.8%** | **100.0%** | **93%** (15) | 94.7% | 96.8% / 94.1% | 0.49s | fail |

Held-out set (`eval/holdout.jsonl`, 36 prompts not used for tuning):

| Model | Download | Action acc. | Off-topic refusal | Delete args | Action+args | Raw model (action / off-topic) | Latency | Result |
|---|---|---|---|---|---|---|---|---|
| `qwen2.5:0.5b` | 398 MB | **86.1%** | **83.3%** | **83%** (6) | 80.6% | 86.1% / 83.3% | 0.45s | fail |
| `qwen2.5:1.5b` | 986 MB | **97.2%** | **100.0%** | **100%** (6) | 97.2% | 94.4% / 83.3% | 0.40s | **PASS** |

Remaining failures for `qwen2.5:1.5b`:

- `rn03` "Can you call report_final.docx report_v3.docx instead?" → expected `rename_path`, got `off_topic`
- `h26` "copy music" → expected `clarify`, got `off_topic`

**How to read these numbers:**
- **"Raw model" is the model's own classification, before the Python checks.** On its own, qwen2.5:1.5b refuses off-topic or injection requests only 70.6% of the time on the main set and 83.3% on the held-out set. Several of those requests it labels as `show_capabilities` or `unclear`. Reaching ≥ 95% depends on the deterministic Python checks: the injection detector, the help/file-cue checks, and path grounding. Those checks are part of the app and always run, but a different model may need the same help.
- **Iterative tuning.** The Python checks were improved iteratively using failures from `dataset.jsonl`. To guard against overfitting, `eval/holdout.jsonl` holds 36 new prompts written afterwards and never used for tuning (results above).
- **Determinism.** Runs use temperature 0 and a fixed seed, so results are close to deterministic but can vary slightly between Ollama versions.

Re-run the evaluation:

```powershell
python eval\evaluate.py --model qwen2.5:1.5b
python eval\evaluate.py --model qwen2.5:0.5b --model qwen2.5:1.5b --stop-at-first-pass
python eval\evaluate.py --dataset eval\holdout.jsonl --model qwen2.5:1.5b
```

The script prints accuracy by category and lists every failure. Full per-prompt results are saved in `eval\results\*.json`.

## Tests

```powershell
python -m pytest                 # unit tests; Ollama is mocked, not needed
python scripts\smoke_test.py     # real end-to-end run with Ollama in a temp workspace
```

The unit tests use temporary directories and cover:
- every handler
- traversal, absolute, UNC, device, reserved-name, ADS and invalid-character blocking
- junction escapes (junctions need no admin rights)
- symlink escapes; these tests **skip with a clear message** unless Developer Mode or admin rights allow creating symlinks
- root protection; confirmations accepted, declined and invalid
- overwrite protection; invalid JSON or schema output
- binary and oversized files; locked files; Ollama unavailable or missing model

The smoke test drives the real CLI through a full scripted session, including 6 escape attempts. It then verifies that a sentinel folder next to the workspace is byte-for-byte unchanged and that nothing new appeared outside the workspace. The transcript is saved to `eval\results\smoke_transcript.txt`. `scripts/smoke_test.py` launches the app with `subprocess`; that is test tooling only, and the app itself never uses it.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot reach Ollama at http://localhost:11434` | Start Ollama from the Start menu or run `ollama serve`. Check with `ollama list`. |
| `Model 'qwen2.5:1.5b' is not installed` | `ollama pull qwen2.5:1.5b` |
| First answer is slow (can take a minute on CPU) | The model is loading into memory. Later answers take about 0.4 s. It stays loaded for 15 minutes. |
| `The model took too long to answer` | Increase `request_timeout` in `filebot.toml` or set `FILEBOT_TIMEOUT=300`. |
| `... is locked or in use by another program` | Close the program that has the file open (Word, Excel, an editor) and retry. |
| `Permission denied` | The file is protected or read-only at the OS level, or the folder needs admin rights. |
| `Sorry, I couldn't understand that` | The model returned invalid output twice. Rephrase as one short command. |
| Colors look wrong in old `cmd.exe` | Use Windows Terminal or PowerShell 7. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or run `.\.venv\Scripts\python.exe -m filebot`. |

## Known limitations

- **One action per message.** "Create a folder and put a file in it" handles only one step.
- **Small-model mistakes.** The model sometimes swaps source and destination, drops a folder from a nested path, or misreads unusual phrasings such as "call X Y instead". Grounding catches invented names but cannot catch a *swap* of two names you did type. Check the summary shown before anything runs; destructive actions always ask first.
- **Grounding is literal.** If you describe a file instead of naming it ("the report from Monday"), FileBot asks for the exact name.
- **Content is literal.** Text written to files must appear in your message, ideally in quotes. FileBot does not compose text for you.
- **Absolute paths inside the workspace are accepted**, but the shortest reliable form is a path relative to the workspace.
- **Links are refused outright.** Symlinks and junctions inside the workspace can't be read, listed or targeted, even when they point inside the workspace.
- **Moves within one volume.** Moves use `os.rename`, so they stay within the workspace's volume (always true for a workspace on one drive).
- **Narrow time window.** Paths are re-checked immediately before each operation, but a process racing with FileBot in the gap between the check and the operation is not fully preventable without OS-level locking.
- **English only**, by design.
