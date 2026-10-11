"""Shared filesystem and strict page-range utilities."""
import hashlib
import json
from pathlib import Path
import re


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def pages(spec, count):
    if count is None or count < 1:
        raise ValueError("缺少有效PDF页数；先运行inventory")
    if spec == "all":
        return list(range(1, count+1))
    selected = set()
    for term in spec.split(","):
        term = term.strip()
        if not re.fullmatch(r"\d+(?:-\d+)?", term):
            raise ValueError(f"无效页范围: {term!r}")
        numbers = [int(v) for v in term.split("-")]
        start, end = numbers[0], numbers[-1]
        if not 1 <= start <= end <= count:
            raise ValueError(f"页范围超界/倒序: {term}")
        selected.update(range(start, end+1))
    if not selected:
        raise ValueError("不能使用空范围")
    return sorted(selected)
