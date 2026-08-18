#!/usr/bin/env python3
"""Turn a Claude Code `--output-format stream-json` transcript into a readable
input -> tool-calls -> output record.

  python3 summarize.py <stream.jsonl> "<prompt>"

Emits Markdown to stdout: the prompt, every tool call the agent made (tool name
+ its input, e.g. the exact curl/python command, plus a snippet of the result),
the final answer, and usage/cost. This is how you see EXACTLY what the agent did.
"""
import json
import sys


def compact_tool_input(name, inp):
    """One-line view of a tool call's input — the actual command / path / args."""
    if not isinstance(inp, dict):
        return str(inp)[:400]
    if name == "Bash":
        return inp.get("command", "")[:600]
    if name in ("Read", "Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Skill":
        return inp.get("skill", inp.get("command", str(inp)))
    if name in ("Grep", "Glob"):
        return json.dumps({k: v for k, v in inp.items() if k in ("pattern", "path", "glob")})
    return json.dumps(inp)[:400]


def result_snippet(content, limit=300):
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", b.get("content", "")))
            else:
                parts.append(str(b))
        content = "\n".join(str(p) for p in parts)
    text = str(content).replace("\n", " ").strip()
    return (text[:limit] + " …") if len(text) > limit else text


def main():
    stream_path, prompt = sys.argv[1], sys.argv[2]
    tool_calls = []          # list of {name, input}
    tool_results = {}        # id -> snippet
    final_text = ""
    usage = {}
    model = ""
    is_error = False

    with open(stream_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                model = ev.get("model", "")
            elif t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        tool_calls.append({"id": block.get("id"), "name": block.get("name"),
                                           "input": block.get("input")})
            elif t == "user":
                for block in ev.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_results[block.get("tool_use_id")] = result_snippet(block.get("content"))
            elif t == "result":
                final_text = ev.get("result", "") or ""
                usage = ev.get("usage", {}) or {}
                is_error = bool(ev.get("is_error"))
                usage["total_cost_usd"] = ev.get("total_cost_usd")
                usage["num_turns"] = ev.get("num_turns")
                usage["duration_ms"] = ev.get("duration_ms")

    out = []
    out.append(f"# eval: {prompt}\n")
    out.append(f"- model: `{model}`")
    out.append(f"- tool calls: {len(tool_calls)}")
    out.append(f"- result: {'ERROR' if is_error else 'ok'}\n")

    out.append("## INPUT\n")
    out.append(prompt + "\n")

    out.append(f"## TOOL CALLS ({len(tool_calls)})\n")
    for i, tc in enumerate(tool_calls, 1):
        out.append(f"### {i}. {tc['name']}")
        out.append("```")
        out.append(compact_tool_input(tc["name"], tc["input"]))
        out.append("```")
        res = tool_results.get(tc["id"])
        if res:
            out.append(f"→ result: {res}\n")
        else:
            out.append("")

    out.append("## FINAL OUTPUT\n")
    out.append(final_text.strip() + "\n")

    out.append("## USAGE")
    out.append("```")
    out.append(json.dumps(usage, indent=2))
    out.append("```")
    print("\n".join(out))


if __name__ == "__main__":
    main()
