#!/usr/bin/env python3
"""Sync MangroveAI copilot ("Michael") skills into this agent's plugin skills.

MangroveAI is the source of truth for these skills. This script copies them from
a MangroveAI checkout into ``.claude/skills/michael/<skill>/`` and adapts them to
this agent's file-resource references. Tool names and server behavior are preserved;
there is no local tool remapping or fallback catalogue. Output is deterministic.

Usage (stdlib only, run from anywhere):

    # Render from a local MangroveAI checkout's working tree
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI

    # Render from a committed ref instead (ignores uncommitted edits in the checkout)
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --ref origin/main

    # No local checkout: fetch a snapshot with gh (needs access to the private repo)
    mkdir -p ~/src/mangroveai-snapshot
    gh api repos/MangroveTechnologies/MangroveAI/tarball/main \\
        | tar -xz --strip-components=1 -C ~/src/mangroveai-snapshot
    python scripts/sync-michael-skills.py --source ~/src/mangroveai-snapshot --commit <sha>

    # Fail (exit 1) if the committed copies differ from what the sync would produce
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --check

    # CI (no MangroveAI access): committed copies match skills-sync-manifest.json
    python scripts/sync-michael-skills.py --verify-manifest

Never hand-edit generated files. Change the upstream skill or the file-resource
adaptation in render_skill and regenerate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEST = REPO_ROOT / ".claude" / "skills" / "michael"
MANIFEST_NAME = "skills-sync-manifest.json"

SOURCE_REPO = "MangroveTechnologies/MangroveAI"
SKILLS_REL = "src/MangroveAI/domains/agent/michael/skills"
PLUGIN_ROOT_REL = ".claude/skills/michael"

# Skills that do not apply to a Claude Code plugin.
#   conversation-memory: Claude Code keeps its own transcript; recall_conversation
#   reads Michael's summarised chat store, which has no equivalent here.
DROP_SKILLS = {"conversation-memory"}


class SyncError(RuntimeError):
    pass


@dataclass
class Source:
    root: Path
    ref: str | None

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def list_files(self, rel_dir: str) -> list[str]:
        if self.ref:
            out = self._git("ls-tree", "-r", "--name-only", self.ref, "--", rel_dir)
            return sorted(p[len(rel_dir) + 1:] for p in out.splitlines() if p)
        base = self.root / rel_dir
        if not base.is_dir():
            raise SyncError(f"{base} not found; --source must be a MangroveAI checkout")
        return sorted(str(p.relative_to(base)) for p in base.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts)

    def read(self, rel_path: str) -> str:
        if self.ref:
            return self._git("show", f"{self.ref}:{rel_path}")
        return (self.root / rel_path).read_text(encoding="utf-8")

    def commit(self, explicit: str | None) -> str | None:
        if explicit:
            return explicit
        try:
            sha = self._git("rev-parse", self.ref or "HEAD").strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        if not self.ref and self._git("status", "--porcelain", "--", SKILLS_REL).strip():
            sha += "+dirty"
        return sha


@dataclass
class Rendered:
    files: dict[str, str] = field(default_factory=dict)          # dest-relative path -> content
    source_hashes: dict[str, str] = field(default_factory=dict)  # source-relative path -> sha256


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_skill(skill: str, text: str, available_tools: set[str]) -> str:
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    if not text.startswith("---\n"):
        raise SyncError(f"{skill}: SKILL.md has no frontmatter")
    end = text.index("\n---\n", 4)
    frontmatter, body = text[4:end], text[end + 5:]
    def declared_tools(match):
        names = [name.strip() for name in match[1].split(',')]
        return 'uses-tools: [' + ', '.join(name for name in names if name in available_tools) + ']'
    frontmatter = re.sub(r'^uses-tools:\s*\[(.*?)\]\s*$', declared_tools, frontmatter, flags=re.M)
    body = body.replace(
        '`load_skill resource="signal_archetype_map.yaml"`',
        f'`${{CLAUDE_PLUGIN_ROOT}}/{PLUGIN_ROOT_REL}/strategy-composition/signal_archetype_map.yaml` (read that file)',
    )
    provenance = (
        f"<!-- Synced from {SOURCE_REPO} {SKILLS_REL}/{skill}/SKILL.md by "
        "scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->"
    )
    boundary = (
        "These instructions describe MangroveAI server tools and server-owned records. "
        "Discover current tools and input schemas through MCP before calling them; "
        "report unavailable capabilities without substituting a local implementation. "
        "Local execution workflows use agent_ tools and local strategy IDs. "
        "Do not pass IDs between those stores."
    )
    return f"---\n{frontmatter}\n---\n\n{provenance}\n\n{boundary}\n{body.rstrip()}\n"


def render(src: Source) -> Rendered:
    available_tools = set(json.loads((REPO_ROOT / 'scripts/contracts/upstream.json').read_text())['tools'])
    rendered = Rendered()
    for rel in src.list_files(SKILLS_REL):
        if "/" not in rel or rel.split("/", 1)[0] in DROP_SKILLS:
            continue
        content = src.read(f"{SKILLS_REL}/{rel}")
        rendered.source_hashes[rel] = _sha(content)
        rendered.files[rel] = (
            render_skill(rel.split("/", 1)[0], content, available_tools)
            if rel.endswith("/SKILL.md") else content.rstrip("\n") + "\n"
        )
    return rendered


def manifest_for(rendered: Rendered, commit: str | None, ref: str | None) -> dict:
    return {
        "generator": "scripts/sync-michael-skills.py",
        "source": {"repo": SOURCE_REPO, "path": SKILLS_REL, "commit": commit, "ref": ref},
        "dropped_skills": sorted(DROP_SKILLS),
        "source_files": dict(sorted(rendered.source_hashes.items())),
        "files": {rel: _sha(text) for rel, text in sorted(rendered.files.items())},
    }


def _on_disk() -> dict[str, str]:
    if not DEST.is_dir():
        return {}
    return {str(p.relative_to(DEST)): p.read_text(encoding="utf-8")
            for p in sorted(DEST.rglob("*")) if p.is_file() and p.name != MANIFEST_NAME}


def write(rendered: Rendered, manifest: dict) -> None:
    existing = _on_disk()
    for rel in sorted(set(existing) - set(rendered.files)):
        (DEST / rel).unlink()
        print(f"removed stale {rel}")
    for rel, text in sorted(rendered.files.items()):
        path = DEST / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing.get(rel) != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {rel}")
    for directory in sorted((p for p in DEST.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    (DEST / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST_NAME} ({len(rendered.files)} files)")


def check(rendered: Rendered, manifest: dict) -> int:
    problems: list[str] = []
    existing = _on_disk()
    for rel in sorted(set(rendered.files) | set(existing)):
        if rel not in existing:
            problems.append(f"missing: {rel}")
        elif rel not in rendered.files:
            problems.append(f"not produced by the sync: {rel}")
        elif existing[rel] != rendered.files[rel]:
            problems.append(f"differs: {rel}")
    manifest_path = DEST / MANIFEST_NAME
    if not manifest_path.is_file():
        problems.append(f"missing: {MANIFEST_NAME}")
    else:
        committed = json.loads(manifest_path.read_text())
        for key in ("files", "source_files", "dropped_skills"):
            if committed.get(key) != manifest[key]:
                problems.append(f"{MANIFEST_NAME}: `{key}` is stale")
        if committed.get("source", {}).get("commit") != manifest["source"]["commit"]:
            print(f"note: manifest records source commit {committed.get('source', {}).get('commit')}, "
                  f"this source is {manifest['source']['commit']} (content is what is compared)")
    for p in problems:
        print(p)
    print("check: OK" if not problems else f"check: FAILED ({len(problems)} problem(s)); re-run the sync")
    return 1 if problems else 0


def verify_manifest() -> int:
    manifest_path = DEST / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"missing {manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text())
    expected: dict[str, str] = manifest.get("files", {})
    actual = {rel: _sha(text) for rel, text in _on_disk().items()}
    problems = [f"missing: {rel}" for rel in sorted(set(expected) - set(actual))]
    problems += [f"not in manifest: {rel}" for rel in sorted(set(actual) - set(expected))]
    problems += [f"hand-edited or stale: {rel}" for rel in sorted(set(expected) & set(actual))
                 if expected[rel] != actual[rel]]
    for p in problems:
        print(p)
    if problems:
        print("verify-manifest: FAILED -- regenerate with scripts/sync-michael-skills.py --source <MangroveAI>")
        return 1
    print(f"verify-manifest: OK ({len(expected)} files match source commit {manifest['source'].get('commit')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, help="MangroveAI checkout (or extracted snapshot) root")
    parser.add_argument("--ref", help="read the skills at this git ref of --source instead of its working tree")
    parser.add_argument("--commit", help="source commit to record when --source is not a git checkout")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if committed copies differ from a fresh sync")
    mode.add_argument("--verify-manifest", action="store_true",
                      help="exit 1 if committed copies differ from skills-sync-manifest.json (no source needed)")
    args = parser.parse_args(argv)

    if args.verify_manifest:
        return verify_manifest()
    if not args.source:
        parser.error("--source is required unless --verify-manifest is given")
    src = Source(args.source.expanduser().resolve(), args.ref)
    try:
        rendered = render(src)
        manifest = manifest_for(rendered, src.commit(args.commit), args.ref)
    except (SyncError, subprocess.CalledProcessError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2
    if args.check:
        return check(rendered, manifest)
    write(rendered, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
