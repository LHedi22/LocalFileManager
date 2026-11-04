"""Evaluate Ollama models on the labeled intent dataset.

Usage (from the project root, with the venv active):
    python eval/evaluate.py --model qwen2.5:1.5b
    python eval/evaluate.py --model qwen2.5:0.5b --model qwen3:0.6b --stop-at-first-pass

Each prompt runs through the real pipeline used by the app: model (structured output,
Pydantic validation, one retry) followed by Python grounding and missing-field checks.
Nothing touches the filesystem. The predicted outcome is the action the app would plan,
or 'clarify', 'off_topic', 'show_capabilities', or 'invalid'.

Pass criteria (from the project spec):
  * action classification accuracy >= 95% (includes refusals and clarifications)
  * off-topic refusal accuracy >= 95%
  * 100% correct path extraction on delete actions
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from filebot.config import load_config  # noqa: E402
from filebot.engine import Engine, Session  # noqa: E402
from filebot.ollama_client import OllamaClient, OllamaError, interpret  # noqa: E402
from filebot.security import Workspace  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
KIND_TO_ACTION = {"refuse": "off_topic", "capabilities": "show_capabilities", "clarify": "clarify", "rephrase": "invalid"}
DELETE_ACTIONS = {"delete_file", "delete_folder"}

console = Console(highlight=False, width=160)


def norm_path(p: str | None) -> str | None:
    if p is None:
        return None
    p = p.replace("\\", "/").strip().strip("/").lower()
    while p.startswith("./"):
        p = p[2:]
    return p or None


def load_dataset(path: Path) -> list[dict]:
    cases = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"dataset line {i}: {exc}")
    return cases


def check_args(expected: dict, intent) -> list[str]:
    problems = []
    for key in ("path", "destination"):
        if key in expected:
            want, got = norm_path(expected[key]), norm_path(getattr(intent, key))
            if key == "destination" and want and got and intent.path:
                # "copy src into backups" -> "backups" and "backups/src" are equivalent
                base = norm_path(intent.path).rsplit("/", 1)[-1]
                if got == f"{want}/{base}":
                    got = want
            if want != got:
                problems.append(f"{key}: expected {expected[key]!r}, got {getattr(intent, key)!r}")
    if "content" in expected:
        want = (expected["content"] or "").strip().lower()
        got = (intent.content or "").strip().lower()
        if want != got:
            problems.append(f"content: expected {expected['content']!r}, got {intent.content!r}")
    if "mode" in expected and expected["mode"] != intent.mode:
        problems.append(f"mode: expected {expected['mode']!r}, got {intent.mode!r}")
    return problems


def evaluate_model(model: str, cases: list[dict], host: str, timeout: float) -> dict:
    client = OllamaClient(host, model, timeout)
    client.check()
    import tempfile

    tmp = tempfile.TemporaryDirectory()
    ws = Workspace(Path(tmp.name) / "ws")
    # Warm-up so model load time does not count toward per-prompt latency.
    interpret(client, "list my files", "Context:\n- (none)")

    rows = []
    latencies = []
    for case in cases:
        session = Session()
        session.remember_paths(*reversed(case.get("context_paths", [])))
        engine = Engine(ws, client, 1_048_576, session)
        start = time.perf_counter()
        interp = interpret(client, case["prompt"], session.context_text())
        latencies.append(time.perf_counter() - start)
        raw_action = interp.intent.action if interp.intent else "invalid"
        exp_action = case["expected"]["action"]
        raw_ok = raw_action == exp_action or (
            exp_action == "clarify" and raw_action not in ("off_topic", "show_capabilities", "invalid")
        )
        if interp.intent is None:
            predicted, intent = "invalid", None
        else:
            resp = engine.analyze(case["prompt"], interp.intent)
            intent = resp.intent
            predicted = KIND_TO_ACTION.get(resp.kind, intent.action if intent else "invalid")
        expected = case["expected"]
        action_ok = predicted == expected["action"]
        arg_problems = check_args(expected, intent) if action_ok and intent is not None and expected["action"] not in KIND_TO_ACTION.values() else []
        rows.append({
            "id": case["id"], "category": case["category"], "prompt": case["prompt"],
            "expected": expected, "predicted": predicted, "raw_model_action": raw_action,
            "intent": intent.model_dump() if intent else None, "retries": len(interp.raw_responses) - 1,
            "raw_action_ok": raw_ok, "action_ok": action_ok, "args_ok": action_ok and not arg_problems, "arg_problems": arg_problems,
        })
    client.close()
    tmp.cleanup()

    def pct(n, d):
        return 100.0 * n / d if d else 100.0

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    off = [r for r in rows if r["expected"]["action"] == "off_topic"]
    false_refusals = [r for r in rows if r["expected"]["action"] != "off_topic" and r["predicted"] == "off_topic"]
    deletes = [r for r in rows if r["expected"]["action"] in DELETE_ACTIONS]
    summary = {
        "model": model,
        "n": len(rows),
        "action_accuracy": pct(sum(r["action_ok"] for r in rows), len(rows)),
        "raw_model_action_accuracy": pct(sum(r["raw_action_ok"] for r in rows), len(rows)),
        "raw_model_off_topic": pct(sum(r["raw_model_action"] == "off_topic" for r in off), len(off)),
        "full_accuracy": pct(sum(r["args_ok"] for r in rows), len(rows)),
        "off_topic_refusal": pct(sum(r["predicted"] == "off_topic" for r in off), len(off)),
        "false_refusals": len(false_refusals),
        "delete_args": pct(sum(r["args_ok"] for r in deletes), len(deletes)),
        "n_delete": len(deletes),
        "invalid_outputs": sum(r["predicted"] == "invalid" for r in rows),
        "retries": sum(r["retries"] for r in rows),
        "avg_latency_s": sum(latencies) / len(latencies),
        "by_category": {
            c: {"n": len(rs), "action": pct(sum(r["action_ok"] for r in rs), len(rs)), "full": pct(sum(r["args_ok"] for r in rs), len(rs))}
            for c, rs in sorted(by_cat.items())
        },
    }
    summary["passed"] = (
        summary["action_accuracy"] >= 95.0 and summary["off_topic_refusal"] >= 95.0 and summary["delete_args"] == 100.0
    )
    return {"summary": summary, "rows": rows}


def print_report(result: dict) -> None:
    s = result["summary"]
    table = Table(title=f"{s['model']} - results by category", header_style="bold")
    table.add_column("Category")
    table.add_column("N", justify="right")
    table.add_column("Action acc.", justify="right")
    table.add_column("Action+args", justify="right")
    for cat, v in s["by_category"].items():
        table.add_row(cat, str(v["n"]), f"{v['action']:.0f}%", f"{v['full']:.0f}%")
    console.print(table)
    verdict = "[bold green]PASS[/]" if s["passed"] else "[bold red]FAIL[/]"
    console.print(
        f"Action classification: {s['action_accuracy']:.1f}%  |  Off-topic refusal: {s['off_topic_refusal']:.1f}%  |  "
        f"Delete args: {s['delete_args']:.1f}% ({s['n_delete']} cases)  |  Action+args: {s['full_accuracy']:.1f}%"
    )
    console.print(
        f"Raw model only (before Python checks): action {s['raw_model_action_accuracy']:.1f}%, "
        f"off-topic {s['raw_model_off_topic']:.1f}%"
    )
    console.print(
        f"False refusals: {s['false_refusals']}  |  Invalid outputs: {s['invalid_outputs']}  |  Retries: {s['retries']}  |  "
        f"Avg latency: {s['avg_latency_s']:.2f}s  |  {verdict}"
    )
    failures = [r for r in result["rows"] if not r["args_ok"]]
    if failures:
        console.print(f"[bold]Failures ({len(failures)}):[/]")
        for r in failures:
            detail = "; ".join(r["arg_problems"]) if r["action_ok"] else f"expected {r['expected']['action']}, got {r['predicted']} (model said {r['raw_model_action']})"
            console.print(f"  [{r['id']}] {r['prompt']!r} -> {detail}", markup=False)
    console.print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", help="model to evaluate (repeatable); default: configured model")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--stop-at-first-pass", action="store_true", help="stop after the first model that passes")
    parser.add_argument("--no-save", action="store_true", help="do not write eval/results/*.json")
    args = parser.parse_args()

    cfg = load_config()
    models = args.model or [cfg.model]
    cases = load_dataset(args.dataset)
    console.print(f"Dataset: {len(cases)} prompts from {args.dataset.name}\n")
    summaries = []
    for model in models:
        console.print(f"[bold]Evaluating {model}...[/]")
        try:
            result = evaluate_model(model, cases, cfg.ollama_host, max(cfg.request_timeout, 300))
        except OllamaError as exc:
            console.print(f"[red]{model}: {exc}[/]")
            continue
        print_report(result)
        summaries.append(result["summary"])
        if not args.no_save:
            RESULTS_DIR.mkdir(exist_ok=True)
            suffix = "" if args.dataset.stem == "dataset" else f".{args.dataset.stem}"
            out = RESULTS_DIR / f"{model.replace(':', '_').replace('/', '_')}{suffix}.json"
            out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if args.stop_at_first_pass and result["summary"]["passed"]:
            break

    if len(summaries) > 1:
        table = Table(title="Model comparison", header_style="bold")
        for col in ("Model", "Action", "Off-topic", "Delete args", "Action+args", "Latency", "Result"):
            table.add_column(col)
        for s in summaries:
            table.add_row(
                s["model"], f"{s['action_accuracy']:.1f}%", f"{s['off_topic_refusal']:.1f}%", f"{s['delete_args']:.1f}%",
                f"{s['full_accuracy']:.1f}%", f"{s['avg_latency_s']:.2f}s", "PASS" if s["passed"] else "fail",
            )
        console.print(table)
    return 0 if any(s["passed"] for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
