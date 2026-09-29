"""Reserve reproducible workload rotations across benchmark invocations."""
from pathlib import Path
from typing import Optional


def rotation_index(root: Path, explicit: Optional[int] = None) -> int:
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or explicit < 0:
            raise ValueError("workload rotation index must be a nonnegative integer")
        return explicit
    import fcntl
    path = root / "results" / ".workload-rotation-index"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as counter:
        fcntl.flock(counter, fcntl.LOCK_EX)
        counter.seek(0)
        index = int(counter.read().strip() or "0")
        if index < 0:
            raise ValueError("stored workload rotation index must be nonnegative")
        counter.seek(0)
        counter.truncate()
        counter.write(str(index + 1))
        counter.flush()
    return index
