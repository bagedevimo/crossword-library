"""Save a raw Puzzmo puzzle payload for local inspection (anonymous request).

Do not commit the output: it is a real, copyrighted Puzzmo puzzle. The committed
test fixtures in tests/fixtures/puzzles are synthetic.

Usage: uv run python scripts/capture_puzzle_fixture.py 2026-10-08/crossword/mini out.json
"""

import json
import sys

from crossword_library.fetch import fetch_raw

url_path, out_path = sys.argv[1], sys.argv[2]
raw = fetch_raw(url_path)
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(raw, fh, indent=1, ensure_ascii=False)
print(f"saved {url_path}: {raw.get('name')!r}")
