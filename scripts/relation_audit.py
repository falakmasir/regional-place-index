"""Diagnose regional relation gaps without confusing them with way-node damage."""
from collections import deque
import datetime as dt
import hashlib
from pathlib import Path


def audit(source, *, supported, inactive, as_of=None):
    import osmium

    today = as_of or dt.datetime.now(dt.timezone.utc).date()
    needed = {kind: set() for kind in ("n", "w", "r")}
    relations = {}
    parents = {}

    class Collect(osmium.SimpleHandler):
        def relation(self, obj):
            tags = dict(obj.tags)
            members = [(str(member.type), int(member.ref)) for member in obj.members]
            for kind, ref in members:
                needed[kind].add(ref)
                if kind == "r":
                    parents.setdefault(ref, set()).add(int(obj.id))
            relations[int(obj.id)] = {
                "id": int(obj.id), "name": tags.get("name", "")[:256],
                "type": tags.get("type", "")[:80], "members": members,
                "indexed_polygon_candidate": bool(tags.get("name", "").strip()
                    and supported(tags) and not inactive(tags, today)
                    and tags.get("type") in {"multipolygon", "boundary"}),
            }

    class Present(osmium.SimpleHandler):
        def node(self, obj): needed["n"].discard(int(obj.id))
        def way(self, obj): needed["w"].discard(int(obj.id))
        def relation(self, obj): needed["r"].discard(int(obj.id))

    # Only retain IDs referenced by relations, not every node in the regional file.
    Collect().apply_file(str(source))
    Present().apply_file(str(source))
    direct = {rid: [(kind, ref) for kind, ref in row["members"] if ref in needed[kind]]
              for rid, row in relations.items()}
    incomplete = {rid for rid, missing in direct.items() if missing}
    todo = deque(sorted(incomplete))
    while todo:
        for parent in parents.get(todo.popleft(), ()):
            if parent not in incomplete:
                incomplete.add(parent)
                todo.append(parent)
    blocked = sorted(rid for rid in incomplete if relations[rid]["indexed_polygon_candidate"])
    samples = []
    for rid in sorted(incomplete)[:50]:
        row = relations[rid]
        samples.append({key: row[key] for key in ("id", "name", "type", "indexed_polygon_candidate")})
        samples[-1].update(
            missing_members=[{"type": kind, "id": ref} for kind, ref in direct[rid][:10]],
            missing_member_count=len(direct[rid]),
            depends_on_incomplete_relation=any(kind == "r" and ref in incomplete for kind, ref in row["members"]),
        )
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "schema": 1, "input_sha256": digest.hexdigest(), "closure_as_of": today.isoformat(),
        "missing_unique_members": {"nodes": len(needed["n"]), "ways": len(needed["w"]), "relations": len(needed["r"])},
        "affected_relation_count": len(incomplete),
        "blocked_polygon_relation_ids": blocked,
        "sample_truncated": len(incomplete) > len(samples), "affected_relations": samples,
    }
