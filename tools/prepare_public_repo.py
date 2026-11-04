"""Export the public file allowlist into a new directory without Git history."""
import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".json", ".md", ".yml", ".yaml", ".toml", ".sha256", ".example"}


def root_document(text):
    """Rebase a docs/public mirror's Markdown links for the repository root."""
    def replace(match):
        target = match.group(1)
        if target.startswith(("https:", "http:", "#")):
            return match.group(0)
        path, marker, fragment = target.partition("#")
        target = posixpath.normpath(posixpath.join("docs/public", path))
        return "](" + target + (marker + fragment if marker else "") + ")"
    return re.sub(r"\]\(([^)]+)\)", replace, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    target = args.destination.resolve()
    if target.exists() or target == ROOT or ROOT in target.parents:
        parser.error("Destination must be a new directory outside this repository")
    recorded = ROOT / "PUBLIC_EXPORT_MANIFEST.json"
    if recorded.is_file():
        names = set(json.loads(recorded.read_text(encoding="utf-8")))
    else:
        names = set()
        for directory in ("video_doc", "tests", "config", "docs/public", "docs/examples", ".github"):
            names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / directory).rglob("*")
                         if p.is_file() and "__pycache__" not in p.parts
                         and p.suffix in {".py", ".json", ".md", ".yml", ".yaml"})
        names.update(name for name in (
            "pyproject.toml", ".gitignore", ".gitattributes", ".env.example",
            "tools/demo_offline.py", "tools/prepare_public_repo.py",
            "tools/build_dev_sample_pack.py", "tools/unpack_dev_samples.py",
            "docs/development/m2-p3-model-role-config-fixture.json",
        ) if (ROOT / name).is_file())
        names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "samples/development").glob("*")
                     if p.is_file() and p.suffix in {".zip", ".sha256", ".json", ".md"})
    target.mkdir(parents=True)
    for name in sorted(names - {"PUBLIC_EXPORT_MANIFEST.json", "README.md", "CONTRIBUTING.md", "AGENTS.md"}):
        source = (ROOT / name).resolve()
        if not source.is_relative_to(ROOT) or not source.is_file():
            raise ValueError(f"Invalid public member: {name}")
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    for name in ("README.md", "CONTRIBUTING.md"):
        content = (ROOT / "docs/public" / name).read_text(encoding="utf-8")
        (target / name).write_bytes(root_document(content).encode("utf-8"))
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if not agents.startswith("# Repository instructions"):
        agents = ("# Repository instructions\n\nRead README.md and docs/public/TEAM_PLAN.md. "
                  "Use Python 3.12 and preserve existing user changes. Keep machine result groups "
                  "compatible and store edits separately. Curated samples are in samples/development/. "
                  "Keep credentials, models and ordinary local outputs out of commits. "
                  "Real model calls require an agreed scope and finite budget.\n")
    (target / "AGENTS.md").write_bytes(agents.encode("utf-8"))
    for path in target.rglob("*"):
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in {".gitignore", ".gitattributes"}):
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    manifest = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(target.rglob("*")) if p.is_file()}
    (target / "PUBLIC_EXPORT_MANIFEST.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode())
    print(f"Prepared {len(manifest)} files at {target}. No Git history or remote operations.")


if __name__ == "__main__":
    main()
