"""Export a reviewed allowlist into a NEW directory, without Git history.

Never changes the source repository, its outputs or remote visibility.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    target = args.destination.resolve()
    if target.exists() or target == ROOT or ROOT in target.parents:
        parser.error('Destination must be a new directory outside this repository')
    files = []
    for directory in ('video_doc', 'tests', 'config', 'docs/public', '.github'):
        files.extend(p for p in (ROOT / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.json', '.md', '.yml'))
    files.extend(ROOT / name for name in (
        'pyproject.toml', '.gitignore', '.env.example',
        'tools/demo_offline.py', 'tools/prepare_public_repo.py',
        'docs/development/m2-p3-model-role-config-fixture.json',
    ))
    target.mkdir(parents=True)
    for source in files:
        dest = target / source.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    shutil.copyfile(ROOT / 'docs/public/README.md', target / 'README.md')
    shutil.copyfile(ROOT / 'docs/public/CONTRIBUTING.md', target / 'CONTRIBUTING.md')
    (target / 'AGENTS.md').write_text(
        '# Repository instructions\n\nRead README.md and CONTRIBUTING.md. Use Python 3.12. '
        'Preserve existing user changes. Run relevant offline tests. Never commit keys, media, '
        'weights or outputs. Paid API calls require explicit user authorization and a finite budget. '
        'Keep M1 compatible and formal result groups immutable. Report unverified items honestly.\n', encoding='utf-8')
    manifest = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(target.rglob('*')) if p.is_file()}
    (target / 'PUBLIC_EXPORT_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Prepared {len(manifest)} files at {target}. No Git history or remote operations.')


if __name__ == '__main__':
    main()
