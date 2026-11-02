"""Deterministic synthetic integration demo; no network or media models."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_document_composer_v2 import _chain, _compose, _ContentTransport, _responses
from tests.test_timeline_builder_v2 import TASK
from video_doc.chapter_frames import select_chapter_frames
from video_doc.document_exporter_v2 import export_document_v2


def main():
    base = Path('data/tmp/offline-demo').resolve()
    base.mkdir(parents=True, exist_ok=True)
    # New workdir avoids destructive cleanup and preserves earlier demonstrations.
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    root = _chain(work)
    select_chapter_frames(TASK, output_root=root)
    _compose(root, _ContentTransport(_responses()), tmp_path=work)
    result = export_document_v2(root, TASK)
    print('Synthetic demo, zero API requests:', result.group_path.resolve())


if __name__ == '__main__':
    main()
