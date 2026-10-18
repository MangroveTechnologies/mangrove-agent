#!/usr/bin/env python3
"""Sync MangroveAI copilot ("Michael") skills into this agent's plugin skills.

MangroveAI is the source of truth for these skills. This script copies them from
a MangroveAI checkout into ``.claude/skills/michael/<skill>/`` and adapts them to
this agent's file-resource references. Tool names and server behavior are preserved;
remote tool names are not remapped. A bounded local Oracle read surface is documented
as a limited fallback for experiment discovery and paginated result inspection; it
does not emulate sweep search or other missing remote capabilities. Output is deterministic.

Usage (stdlib only, run from anywhere):

    # Render the full reviewed source catalog (default)
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI

    # Render for the target deployment's fresh observed catalog
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --catalog-mode deployed

    # Render from a committed ref instead (ignores uncommitted edits in the checkout)
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --ref origin/main

    # No local checkout: fetch a snapshot with gh (needs access to the private repo)
    mkdir -p ~/src/mangroveai-snapshot
    gh api repos/MangroveTechnologies/MangroveAI/tarball/main \\
        | tar -xz --strip-components=1 -C ~/src/mangroveai-snapshot
    python scripts/sync-michael-skills.py --source ~/src/mangroveai-snapshot --commit <sha>

    # Check the target deployment profile
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --catalog-mode deployed --check

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
from datetime import datetime, timedelta, timezone
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
DROP_SKILLS = {"conversation-memory", "knowledge-graph"}
CATALOG_MAX_AGE = timedelta(days=14)


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
    skill_tools: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalog:
    mode: str
    names: set[str]
    metadata: dict
    advertised_names: set[str]


def load_catalog(mode: str, now: datetime | None = None, enforce_freshness: bool = True) -> Catalog:
    """Load the full reviewed source catalog or a fresh observed deployment catalog."""
    if mode == "source":
        path = REPO_ROOT / "scripts/contracts/upstream.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SyncError("Reviewed source catalog snapshot is unavailable") from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SyncError("Malformed reviewed source catalog") from exc
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("tools"), dict)):
            raise SyncError("Malformed reviewed source catalog")
        names = set(data["tools"])
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", name)
               for name in names):
            raise SyncError("Malformed reviewed source tool names")
        return Catalog("source", names, {
            "snapshot": "scripts/contracts/upstream.json",
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "tool_count": len(names),
        }, names)
    if mode != "deployed":
        raise SyncError(f"Unknown catalog mode: {mode}")

    path = REPO_ROOT / "scripts/contracts/deployed.json"
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SyncError("Malformed deployed catalog snapshot") from exc
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or not isinstance(data.get("source"), str)
            or not isinstance(data.get("observed_at_utc"), str)
            or type(data.get("tool_count")) is not int or not isinstance(data.get("tools"), list)):
        raise SyncError("Malformed deployed catalog snapshot")
    names = data["tools"]
    if (len(names) != data["tool_count"] or len(names) > 4096
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", name)
                   for name in names)
            or len(names) != len(set(names))):
        raise SyncError("Invalid deployed catalog tool names or count")
    try:
        observed = datetime.fromisoformat(data["observed_at_utc"])
    except ValueError as exc:
        raise SyncError("Invalid deployed catalog timestamp") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise SyncError("Deployed catalog timestamp must include a UTC offset")
    observed = observed.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    if enforce_freshness:
        if observed > current + timedelta(minutes=5):
            raise SyncError("Deployed catalog snapshot timestamp is in the future")
        if current - observed > CATALOG_MAX_AGE:
            raise SyncError(f"Deployed catalog snapshot is stale (older than {CATALOG_MAX_AGE.days} days)")
    observed_names = set(names)
    reviewed_names = load_catalog("source").names
    return Catalog("deployed", observed_names, {
        "snapshot": "scripts/contracts/deployed.json",
        "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "observed_at_utc": data["observed_at_utc"],
        "source": data["source"],
        "tool_count": data["tool_count"],
    }, observed_names & reviewed_names)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_skill(skill: str, text: str, available_tools: set[str], catalog_mode: str = "source",
                 observed_tools: set[str] | None = None) -> str:
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    if not text.startswith("---\n"):
        raise SyncError(f"{skill}: SKILL.md has no frontmatter")
    end = text.index("\n---\n", 4)
    frontmatter, body = text[4:end], text[end + 5:]
    declared = re.search(r'^uses-tools:\s*\[(.*?)\]\s*$', frontmatter, flags=re.MULTILINE)
    source_tools = [name.strip() for name in declared[1].split(',') if name.strip()] if declared else []
    selected_tools = [name for name in source_tools if name in available_tools]
    observed_tools = available_tools if observed_tools is None else observed_tools
    missing_tools = [name for name in source_tools if name not in observed_tools]
    unreviewed_tools = [name for name in source_tools if name in observed_tools and name not in available_tools]
    if declared:
        frontmatter = (frontmatter[:declared.start()] + 'uses-tools: [' + ', '.join(selected_tools) + ']'
                       + frontmatter[declared.end():])
    frontmatter += '\nsource-uses-tools: [' + ', '.join(source_tools) + ']'
    frontmatter += '\nunavailable-tools: [' + ', '.join(missing_tools) + ']'
    frontmatter += '\nunreviewed-tools: [' + ', '.join(unreviewed_tools) + ']'
    frontmatter += f'\ncatalog-mode: {catalog_mode}'
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
        "When a remote Oracle experiment/result reader is unavailable, the authenticated "
        "local agent_list_oracle_experiments and agent_list_oracle_results tools can "
        "discover experiment summaries and read bounded result pages only. They do not "
        "provide sweep-specific filtering, ranking, or mutation; never present them as "
        "equivalent to a missing remote tool. "
        "Local execution workflows use agent_ tools and local strategy IDs. "
        "Do not pass IDs between those stores."
    )
    capability_note = ""
    if catalog_mode == "deployed" and missing_tools:
        capability_note = (
            "\n\nCatalog snapshot note (observed deployed tools only): this skill references "
            "tools absent from the recorded catalog snapshot: "
            + ", ".join(f"`{name}`" for name in missing_tools)
            + ". Do not call or replace these by assumption. Check the active MCP `tools/list`; "
            "use a capability only when that live catalog exposes its schema."
        )
    if catalog_mode == "deployed" and unreviewed_tools:
        capability_note += (
            "\n\nCatalog review note: these source tools appear in the observed deployment but "
            "are not in the reviewed source contract, so this bundle does not advertise them: "
            + ", ".join(f"`{name}`" for name in unreviewed_tools)
            + ". Their schemas were not validated by this snapshot; inspect the active MCP `tools/list` "
            "schema before use."
        )
    return f"---\n{frontmatter}\n---\n\n{provenance}\n\n{boundary}{capability_note}\n{body.rstrip()}\n"


def render(src: Source, catalog: Catalog | None = None) -> Rendered:
    catalog = catalog or load_catalog("source")
    available_tools = catalog.advertised_names
    rendered = Rendered()
    for rel in src.list_files(SKILLS_REL):
        if "/" not in rel or rel.split("/", 1)[0] in DROP_SKILLS:
            continue
        content = src.read(f"{SKILLS_REL}/{rel}")
        rendered.source_hashes[rel] = _sha(content)
        if rel.endswith("/SKILL.md"):
            skill = rel.split("/", 1)[0]
            match = re.search(r'^uses-tools:\s*\[(.*?)\]\s*$', content, flags=re.MULTILINE)
            source_tools = [name.strip() for name in match[1].split(',') if name.strip()] if match else []
            rendered.skill_tools[skill] = {
                "source": source_tools,
                "advertised": [name for name in source_tools if name in available_tools],
                "unavailable": [name for name in source_tools if name not in catalog.names],
                "unreviewed": [name for name in source_tools if name in catalog.names and name not in available_tools],
            }
            rendered.files[rel] = render_skill(skill, content, available_tools, catalog.mode, catalog.names)
        else:
            rendered.files[rel] = content.rstrip("\n") + "\n"
    return rendered


def manifest_for(rendered: Rendered, commit: str | None, ref: str | None, catalog: Catalog) -> dict:
    source_names = set(json.loads((REPO_ROOT / "scripts/contracts/upstream.json").read_text())["tools"])
    return {
        "generator": "scripts/sync-michael-skills.py",
        "source": {"repo": SOURCE_REPO, "path": SKILLS_REL, "commit": commit, "ref": ref},
        "dropped_skills": sorted(DROP_SKILLS),
        "catalog": {
            **catalog.metadata,
            "mode": catalog.mode,
            "available_tools": sorted(catalog.names),
            "advertised_tools": sorted(catalog.advertised_names),
            "missing_expected_tools": sorted(source_names - catalog.names),
            "unreviewed_observed_tools": sorted(catalog.names - source_names),
        },
        "skill_tools": {name: rendered.skill_tools[name] for name in sorted(rendered.skill_tools)},
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
        for key in ("files", "source_files", "dropped_skills", "catalog", "skill_tools"):
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
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid {MANIFEST_NAME}: {exc}")
        return 1
    expected: dict[str, str] = manifest.get("files", {})
    actual = {rel: _sha(text) for rel, text in _on_disk().items()}
    problems = [f"missing: {rel}" for rel in sorted(set(expected) - set(actual))]
    problems += [f"not in manifest: {rel}" for rel in sorted(set(actual) - set(expected))]
    problems += [f"hand-edited or stale: {rel}" for rel in sorted(set(expected) & set(actual))
                 if expected[rel] != actual[rel]]
    try:
        catalog_mode = manifest["catalog"]["mode"]
        catalog = load_catalog(catalog_mode, enforce_freshness=False)
        source_names = load_catalog("source", enforce_freshness=False).names
        expected_catalog = {
            **catalog.metadata,
            "mode": catalog.mode,
            "available_tools": sorted(catalog.names),
            "advertised_tools": sorted(catalog.advertised_names),
            "missing_expected_tools": sorted(source_names - catalog.names),
            "unreviewed_observed_tools": sorted(catalog.names - source_names),
        }
        if manifest.get("catalog") != expected_catalog:
            problems.append("catalog metadata does not match its referenced snapshot")
        for skill, tools in manifest.get("skill_tools", {}).items():
            path = DEST / skill / "SKILL.md"
            if not path.is_file():
                problems.append(f"missing skill metadata source: {skill}/SKILL.md")
                continue
            frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
            fields = {}
            for field_name in ("uses-tools", "source-uses-tools", "unavailable-tools", "unreviewed-tools"):
                match = re.search(rf"^{field_name}:\s*\[(.*?)\]\s*$", frontmatter, flags=re.MULTILINE)
                fields[field_name] = [name.strip() for name in match[1].split(",") if name.strip()] if match else None
            source = tools.get("source")
            if not isinstance(source, list):
                problems.append(f"invalid source tool metadata: {skill}")
                continue
            expected_tools = {
                "source": source,
                "advertised": [name for name in source if name in catalog.advertised_names],
                "unavailable": [name for name in source if name not in catalog.names],
                "unreviewed": [name for name in source if name in catalog.names and name not in catalog.advertised_names],
            }
            frontmatter_tools = {
                "source": fields["source-uses-tools"],
                "advertised": fields["uses-tools"],
                "unavailable": fields["unavailable-tools"],
                "unreviewed": fields["unreviewed-tools"],
            }
            if tools != expected_tools or frontmatter_tools != expected_tools:
                problems.append(f"skill tool metadata does not match catalog profile: {skill}")
    except (KeyError, TypeError, SyncError, OSError, json.JSONDecodeError) as exc:
        problems.append(f"invalid catalog/skill metadata: {exc}")
    for p in problems:
        print(p)
    if problems:
        print("verify-manifest: FAILED -- regenerate with scripts/sync-michael-skills.py --source <MangroveAI> --catalog-mode <source|deployed>")
        return 1
    print(f"verify-manifest: OK ({len(expected)} files match source commit {manifest['source'].get('commit')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, help="MangroveAI checkout (or extracted snapshot) root")
    parser.add_argument("--ref", help="read the skills at this git ref of --source instead of its working tree")
    parser.add_argument("--commit", help="source commit to record when --source is not a git checkout")
    parser.add_argument("--catalog-mode", choices=("source", "deployed"), default="source",
                        help="advertise the reviewed source catalog (default) or a fresh deployed observation")
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
        catalog = load_catalog(args.catalog_mode)
        rendered = render(src, catalog)
        manifest = manifest_for(rendered, src.commit(args.commit), args.ref, catalog)
    except (SyncError, subprocess.CalledProcessError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2
    if args.check:
        return check(rendered, manifest)
    print(f"catalog mode={catalog.mode}; snapshot={catalog.metadata['snapshot']}; "
          f"sha256={catalog.metadata['snapshot_sha256']}; tools={len(catalog.names)}"
          + (f"; observed_at_utc={catalog.metadata['observed_at_utc']}" if catalog.mode == "deployed" else ""))
    write(rendered, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
