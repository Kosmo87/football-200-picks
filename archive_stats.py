"""What the archive currently holds. Run it to see the dataset growing."""
from collections import defaultdict
import glob, json, os

from archive import ARCHIVE_DIR, read_ndjson

print(f"archive: {ARCHIVE_DIR}\n")
for path in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*.ndjson"))):
    rows = read_ndjson(path)
    name = os.path.basename(path)
    size = os.path.getsize(path) / 1024
    if name.startswith("lines"):
        by_event = defaultdict(list)
        for r in rows:
            by_event[r["event_id"]].append(r)
        moves = [len(v) - 1 for v in by_event.values()]
        print(f"{name:<34} {len(rows):>6} rows  {size:>7.0f} KB")
        print(f"  {len(by_event)} games · "
              f"{sum(moves)} line moves captured · "
              f"{sum(moves)/len(by_event):.1f} per game" if by_event else "  empty")
    else:
        players = sum(len(r.get("players") or []) for r in rows)
        print(f"{name:<34} {len(rows):>6} games {size:>7.0f} KB")
        print(f"  {players} player statlines")
print("""
Every price move on a game is one row, so the file grows only when the market
actually moves. A season lands in the low tens of megabytes rather than the
hundreds an hourly dump would cost.""")
