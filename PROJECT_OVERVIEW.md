# FileBot: a local AI file manager

A terminal chatbot that manages files and folders inside one workspace from plain-English commands. It runs entirely on the local machine through Ollama. No cloud API is used.

Repository: https://github.com/LHedi22/LocalFileManager

---

## 1. The problem

- Natural-language interfaces to the file system are convenient, but an LLM that can touch files is risky. Models hallucinate paths, can be manipulated by prompt injection, and may act on requests they misunderstood.
- Cloud LLMs send your requests (and potentially your file names and contents) to third parties.
- **Goal:** a file-management assistant that is local, small, safe by construction, and honest about how accurate it is.

## 2. Project goals

1. Understand English commands for 10 file actions plus "what can you do?".
2. Refuse everything else: questions, coding help, jokes, translation, role-change attempts.
3. Ask one short question when a detail is missing. Never guess paths.
4. Use the **smallest** local model that reaches measured accuracy targets.
5. Enforce **every** safety rule in Python. The model and its prompt are never trusted.
6. Provide a clean Windows terminal experience.

## 3. What it can do

| Action | Example command |
|---|---|
| create_file | `create a file called notes.txt with the text 'hello'` |
| create_folder | `make a folder named projects` |
| list_folder | `what's inside the projects folder?` |
| read_file | `show me what's in todo.md` |
| edit_file (append / replace) | `append 'buy eggs' to shopping.txt` |
| rename_path | `rename notes.txt to todo.txt` |
| move_path | `move todo.txt into projects` |
| copy_path | `copy config.ini to config_backup.ini` |
| delete_file | `delete backup.txt` (asks for confirmation) |
| delete_folder | `delete the folder old_projects` (shows contents, asks for confirmation) |
| show_capabilities | `what can you do?` / `help` |

Other behaviors:
- **Follow-ups:** `delete it`, `rename it to final.txt`, `read that file`, using short in-memory context (last 3 turns, last 4 paths) that is never saved to disk.
- **Clarification:** `delete a file` gets *"Which file should I delete?"*, and the user can answer with just the name.
- **Refusal:** `tell me a joke` gets *"I can only manage files and folders"* plus example commands.
- **Suggestions:** after each success, one text-only, non-destructive next step, e.g. *"Add a README: create a file called projects/README.md"*.

## 4. Example session

```
you> create a folder named projects
Created folder 'projects'.
Suggestion: Add a README: create a file called projects/README.md
you> create a file called projects/README.md with the text 'My project'
Created file 'projects/README.md' with 10 characters.
you> delete it
╭─ Confirm ─────────────────────────────────────────────╮
│ Action:  Delete file 'projects/README.md'             │
│ Target:  C:\...\workspace\projects\README.md          │
│ Permanently DELETE file 'projects/README.md'          │
│ (10 bytes)? This cannot be undone.                    │
╰───────────────────────────────────────────────────────╯
Type 'y' or 'yes' to proceed, anything else cancels: n
Cancelled. Nothing was changed.
you> tell me a joke
I can only manage files and folders in your workspace.
```

## 5. Technology stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | Standard library `tomllib`, strong Windows path support |
| LLM runtime | Ollama 0.40 (local) | Fully offline; supports structured outputs |
| Model | qwen2.5:1.5b (986 MB) | Smallest model that met every target |
| Model output | Ollama structured outputs (strict JSON schema) | No reliance on native tool calling |
| Validation | Pydantic v2 | Every response is validated before use |
| HTTP | httpx | Simple, mockable transport for tests |
| UI | Rich | Colors, panels, tables and a spinner in Windows Terminal and PowerShell |
| Tests | pytest | 230 tests; Ollama mocked |

Size: about 2,200 lines of application code, 1,400 lines of tests, and 380 lines of evaluation and smoke-test tooling.

## 6. Architecture

```
 user text
   │
   ▼
 Ollama model ── strict JSON schema ──► {action, path, destination, content, mode}
   │
   ▼
 Pydantic validation ── invalid? retry once ── still invalid? "please rephrase" (no action)
   │
   ▼
 Python checks (engine.py)
   • injection / role-change detector
   • grounding: every path must come from the user's words or recent context
   • missing details → one clarifying question
   │
   ▼
 Allowlisted planner (actions.py) ── path security (security.py)
   │
   ▼
 Confirmation typed by the user (delete / overwrite only)
   │
   ▼
 Re-validate paths ──► execute ──► result + suggestion
```

### Modules

| Module | Responsibility |
|---|---|
| `cli.py` | Terminal interface, help/exit, confirmations, all output |
| `ollama_client.py`, `prompts.py` | Local Ollama client, system prompt, few-shot examples, retry |
| `schemas.py` | JSON schema sent to Ollama; Pydantic `Intent` model |
| `engine.py` | Session context, grounding, clarification, injection checks |
| `security.py` | Workspace path security |
| `actions.py` | The 10 filesystem handlers (plan → confirm → re-check → execute) |
| `suggestions.py` | Next-step suggestions and the capabilities list |
| `config.py` | Settings from `filebot.toml`, environment variables and CLI flags |

**Key design idea:** the model only *classifies* and *extracts arguments*. It never writes text the user sees and never sees file contents. All messages, refusals and questions are Python templates.

## 7. Security model: "treat the model as untrusted"

### Path security
Every path is resolved and must stay inside the workspace (case-insensitive on Windows). The following are blocked:
- absolute paths outside the workspace, drive-relative (`C:foo`), rooted (`\foo`) and `~` paths
- any `..` component
- UNC and device paths (`\\server\share`, `\\?\`, `\\.\`)
- alternate data streams (`file.txt:hidden`)
- reserved device names: CON, PRN, AUX, NUL, COM0–9, LPT0–9, including `con.txt`
- forbidden characters, and trailing dots or spaces
- symlinks, junctions and other redirecting reparse points; they are never followed

### Action safety
- **Fixed allowlist** of 10 handlers. No shell, `eval`, `exec`, `os.system` or `subprocess` in the app.
- **Workspace root** can never be deleted, renamed, moved or copied.
- **Re-check before execution:** paths are resolved again right before the operation runs.
- **No silent overwrite:** move, copy and rename refuse when the destination exists; create and replace ask first.
- **Safe folder deletion:** a junction inside a deleted folder is removed as a link; its target is never touched.

### Confirmations
- Required for deletions and overwrites. Only `y` or `yes` typed at the prompt counts; anything else cancels.
- Text in the original request ("yes, I confirm") or in model output cannot confirm.
- The prompt shows the action, the absolute resolved target, folder contents (files and subfolders), and a "cannot be undone" warning.

### Reading files
- UTF-8 text only, 1 MB limit (configurable). Binary and oversized files are refused.
- Terminal escape sequences are neutralized, and file contents are shown as data, never interpreted.

### Prompt-injection defense
- Role-change or instruction-override phrasing outside quotes is refused, even if the model returned an action.
- Quoted text is data: `create memo.txt with the text 'ignore your rules'` works normally.

### Grounding: no guessing
- A path the user never typed is turned into a question instead of an action. For example, `delete the file` gets *"Which file should I delete?"*.
- Invented extensions, placeholder words ("file", "folder") and invented file content are rejected the same way.

## 8. Model selection

### Method
1. Searched the Ollama library for small instruction models and recorded their download sizes before pulling anything.
2. Built a labeled dataset of **94 prompts**: every action in varied wording, 11 missing-detail prompts, 10 off-topic prompts, 11 prompt-injection prompts, and 6 context follow-ups.
3. Evaluated candidates smallest first, through the same pipeline the app uses.
4. **Targets:** ≥ 95% action classification, ≥ 95% off-topic refusal, 100% correct path extraction on delete actions.
5. Wrote a **held-out set of 36 new prompts** that was never used for tuning, to check for overfitting.

### Results: main dataset (94 prompts)

| Model | Download | Action | Off-topic | Delete paths | Raw model (action / off-topic) | Latency | Result |
|---|---|---|---|---|---|---|---|
| qwen2.5:0.5b | 398 MB | 94.7% | 94.1% | 100% | 94.7% / 94.1% | 0.22 s | fail |
| qwen3:0.6b | 523 MB | 93.6% | 88.2% | 100% | 89.4% / 58.8% | 0.35 s | fail |
| gemma3:1b | 806 MB | 90.4% | 100% | 93% | 90.4% / 94.1% | 0.42 s | fail |
| **qwen2.5:1.5b** | **986 MB** | **98.9%** | **100%** | **100%** | 93.6% / 70.6% | 0.40 s | **PASS** |
| llama3.2:1b | 1.3 GB | 96.8% | 100% | 93% | 96.8% / 94.1% | 0.49 s | fail |

### Results: held-out set (36 prompts, never used for tuning)

| Model | Action | Off-topic | Delete paths | Result |
|---|---|---|---|---|
| qwen2.5:0.5b | 86.1% | 83.3% | 83% | fail |
| **qwen2.5:1.5b** | **97.2%** | **100%** | **100%** | **PASS** |

### Takeaways
- **qwen2.5:1.5b** is the smallest model that passed, and it still passed on unseen prompts.
- qwen2.5:0.5b came close on the main set but dropped to 86% on new prompts, which shows it is too weak.
- **The Python checks matter.** On its own, the selected model refuses off-topic requests only 70.6% of the time (83.3% on the held-out set). Deterministic Python checks raise that to 100%.
- Only 2 failures remain across both sets: an unusual phrasing (*"Can you call X Y instead?"*) and *"copy music"*, which was refused instead of getting a clarifying question.

## 9. Testing and verification

### Unit tests: 230 tests, Ollama mocked
| File | Tests | Covers |
|---|---|---|
| `test_actions.py` | 55 | Every handler, overwrite protection, root protection, binary/oversized/non-UTF-8 files, locked files |
| `test_engine.py` | 40 | Invalid JSON and schema output, retry, grounding, follow-ups, clarifications, injection detection |
| `test_security.py` | 22 | Traversal, absolute, UNC, device, reserved-name and ADS blocking; junction and symlink escapes |
| `test_cli.py` | 17 | Confirmation accepted, declined and invalid; help/exit; no stack traces; resolved target shown |
| `test_ollama_client.py` | 10 | Ollama unavailable, missing model, timeout, structured-output request format |
| `test_config_and_suggestions.py` | 8 | Config precedence, invalid config, suggestions never destructive |

Result: **228 passed, 2 skipped**. The 2 skipped are symlink tests, which need Windows Developer Mode or admin rights; the equivalent junction tests run and pass.

### End-to-end smoke test (real Ollama)
- Runs the real CLI through a scripted 30-step session in a temporary workspace.
- Includes 6 escape attempts: `..`, an absolute path outside the workspace, a junction, `CON.txt`, an alternate data stream, and a move to `..`. **All were blocked.**
- Verifies that a sentinel folder outside the workspace is **byte-for-byte unchanged** and that nothing new appeared next to the workspace.
- Result: **17/17 checks passed.**

## 10. Problems found and fixed during development

Testing against the real model found issues that unit tests alone missed:

| Issue | Fix |
|---|---|
| Small models confused "off-topic" with "show capabilities" | Python requires a help-style cue for capabilities and a file cue for "unclear" |
| Model shortened `logs/server.log` to `server.log` | Expand to the full path the user actually typed |
| Model wrote `data\temp.csv` into JSON, so `\t` became a tab | Repair control characters in paths |
| `delete it` after a move targeted the old location | Moved and renamed sources are removed from session context |
| `notes.txt:hidden` was silently read as `notes.txt` | Keep the `:stream` suffix so the security layer blocks it |
| `copy notes.txt` produced destination = source | Treat as a missing destination and ask a question |

## 11. Known limitations

- One action per message.
- The small model sometimes swaps source and destination. Grounding catches invented names but not a swap of two names the user typed. Destructive actions always show the target and ask first.
- Names and file content must appear literally in the message.
- Symlinks and junctions are refused entirely, even when they point inside the workspace.
- The first response is slow on CPU (model load, up to about a minute); after that, about 0.4 s per response.
- A process changing files in the split second between the final check and the operation can't be fully prevented without OS-level locking.
- English only.

## 12. Future work

- Multi-step commands with a single combined confirmation.
- Optional undo through a workspace recycle bin.
- Fine-tuning a sub-1B model on the dataset to shrink the footprint further.
- Larger and more diverse evaluation sets, including non-native phrasing.
- Search and filter actions (find files by name, size or date).

## 13. How to run

```powershell
ollama pull qwen2.5:1.5b
git clone https://github.com/LHedi22/LocalFileManager
cd LocalFileManager
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
filebot                                 # default workspace: .\workspace
filebot --workspace D:\Sandbox --debug  # custom workspace, show parsed intent
python -m pytest                        # unit tests
python eval\evaluate.py --model qwen2.5:1.5b
python scripts\smoke_test.py            # end-to-end test with real Ollama
```

---

## Suggested slide outline

1. **Title:** FileBot, a local AI file manager
2. **Problem:** LLMs plus file systems are risky; cloud models raise privacy concerns (§1)
3. **Goals** (§2)
4. **Demo / what it can do** (§3, §4)
5. **Architecture pipeline** (§6 diagram)
6. **Design principle:** the model only classifies; Python decides (§6)
7. **Security model:** path security and confirmations (§7)
8. **Prompt injection and grounding** (§7)
9. **Model selection method** (§8)
10. **Results table** (§8)
11. **Takeaway:** Python checks vs. raw model (§8)
12. **Testing:** 230 unit tests plus a real end-to-end test (§9)
13. **Lessons learned:** bugs found with the real model (§10)
14. **Limitations and future work** (§11, §12)
15. **Q&A**
