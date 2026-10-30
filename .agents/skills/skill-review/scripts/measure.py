"""Measure one Agent Skill directory for the skill-review rubric.

Usage: ``python3 measure.py SKILL_DIR``. Prints the name, the description length, the
quoted triggers after ``Use when the user says``, the body line count and every relative
markdown link in ``SKILL.md`` with its line count, then one flag per line. Stdlib only.
Exit 2 when ``SKILL_DIR`` has no ``SKILL.md`` or its frontmatter does not open and close;
else exit 0: the flags are data for the reviewer, not a verdict.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = "Use when the user says"
BODY_LIMIT = 500
DESCRIPTION_LIMIT = 1024
CONTENTS_THRESHOLD = 100
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_QUOTED = re.compile(r'"([^"]+)"')
_CLAUDE_ONLY = ("$ARGUMENTS", "CLAUDE_SKILL_DIR", "when_to_use", "```!")


def read_skill(path: Path) -> tuple[dict[str, str], list[str]]:
    """Unindented ``key: value`` lines of the frontmatter (indented lines fold into the
    previous value), and the body lines after the closing ``---``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter not opened")
    head: dict[str, str] = {}
    key = ""
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return head, lines[index + 1 :]
        if line and not line[0].isspace() and ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            head[key] = "" if value in (">", "|", ">-", "|-") else value
        elif line.strip() and key:
            head[key] = f"{head[key]} {line.strip()}".strip()
    raise ValueError("frontmatter not closed")


def links(lines: list[str]) -> list[str]:
    """Relative link targets outside fenced code, without anchors, in order, once each."""
    found: list[str] = []
    fenced = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        for target in _LINK.findall(line):
            target = target.split("#", 1)[0]
            if target and "://" not in target and target not in found:
                found.append(target)
    return found


def has_contents(lines: list[str]) -> bool:
    return any(line.strip().lower() == "## contents" for line in lines[:20])


def yaml_hazard(description: str) -> bool:
    plain = not description.startswith(("'", '"'))
    return (plain and (": " in description or " #" in description)) or bool(
        set(description) & {"<", ">"}
    )


def measure(skill_dir: Path) -> list[str]:
    head, body = read_skill(skill_dir / "SKILL.md")
    description = head.get("description", "")
    marker = description.find(MARKER)
    quoted = _QUOTED.findall(description[marker + len(MARKER) :]) if marker >= 0 else []
    out = [
        f"name: {head.get('name', '')}",
        f"description: {len(description)}/{DESCRIPTION_LIMIT} characters",
        f"triggers: {len(quoted)}",
        *(f"  - {phrase}" for phrase in quoted),
        f"body: {len(body)} lines",
        "links:",
    ]
    flags: list[str] = []
    root = skill_dir.resolve()
    for target in links(body):
        path = (skill_dir / target).resolve()
        if not path.is_file():
            out.append(f"  {target}: missing")
            flags.append(f"MISSING_LINK {target}")
            continue
        text = path.read_text(encoding="utf-8").splitlines()
        out.append(f"  {target}: {len(text)} lines")
        if len(text) > CONTENTS_THRESHOLD and not has_contents(text):
            flags.append(f"NO_CONTENTS {target}")
        if path.is_relative_to(root) and path.suffix == ".md":
            for inner in links(text):
                nested = (path.parent / inner).resolve()
                if nested.suffix == ".md" and nested.is_relative_to(root):
                    flags.append(f"NESTED_LINK {target} -> {inner}")
    if len(body) >= BODY_LIMIT:
        flags.append("OVER_500")
    if len(description) > DESCRIPTION_LIMIT:
        flags.append("DESC_OVER_1024")
    if yaml_hazard(description):
        flags.append("YAML_HAZARD")
    whole = "\n".join(body) + "\n" + "\n".join(f"{k}: {v}" for k, v in head.items())
    flags.extend(f"CLAUDE_ONLY {token}" for token in _CLAUDE_ONLY if token in whole)
    if head.get("name") != skill_dir.resolve().name:
        flags.append(f"NAME_MISMATCH {head.get('name', '')} != {skill_dir.resolve().name}")
    out.append("flags:")
    out.extend(f"  {flag}" for flag in flags or ["none"])
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: measure.py SKILL_DIR", file=sys.stderr)
        return 2
    skill_dir = Path(argv[0])
    if not (skill_dir / "SKILL.md").is_file():
        print(f"Error: no SKILL.md in {skill_dir}", file=sys.stderr)
        return 2
    try:
        lines = measure(skill_dir)
    except ValueError as exc:
        print(f"Error: {skill_dir / 'SKILL.md'}: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
