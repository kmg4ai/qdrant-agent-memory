#!/usr/bin/env python3
# Ingests documentation into Qdrant — RAM-safe: 20 facts/batch + gc.collect()
import os, sys, re, gc, hashlib, subprocess
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from fastembed import TextEmbedding
from datetime_utils import content_ts, time_features

# Secret Guard — redaction of secrets in all facts before storing
from secret_guard import scrub

_BATCH_SIZE = 10


client = QdrantClient(
    url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"), timeout=60
)
COLLECTION = os.getenv("COLLECTION_NAME")

# --replace forces a full re-ingest (clear + upload everything) instead of incremental append
FORCE_REPLACE = "--replace" in sys.argv

# ===== SOURCE PATHS (configurable via env; defaults are typical locations) =====
VPS_DOC_PATH = os.getenv("QDRANT_VPS_DOC", os.path.expanduser("~/VPS.md"))
CHANGELOG_PATH = os.getenv("QDRANT_CHANGELOG", "CHANGELOG.md")
WWW_ROOT = os.getenv("QDRANT_WWW_ROOT", "/var/www")
NGINX_DIR = os.getenv("QDRANT_NGINX_DIR", "/etc/nginx/sites-enabled")
SYSTEMD_DIR = os.getenv("QDRANT_SYSTEMD_DIR", "/etc/systemd/system")


def clear_source(source: str):
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source))]
        ),
    )
    print(f"  Removed old entries for source={source}")


def point_id(source: str, text: str) -> int:
    # Content-based ID — the same text always yields the same ID, regardless of
    # its position in the file. This way a new entry at the top of CHANGELOG
    # does not shift the IDs of the remaining entries.
    return (
        int(hashlib.sha256(f"{source}|{text}".encode()).hexdigest()[:16], 16)
        & 0x7FFFFFFFFFFFFFFF
    )


def get_existing_ids(source: str) -> set:
    # All point IDs of a source present in Qdrant (fast scroll, no payload)
    ids = set()
    offset = None
    while True:
        res = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="source", match=MatchValue(value=source))]
            ),
            limit=1000,
            with_payload=False,
            offset=offset,
        )
        points, offset = res
        for p in points:
            ids.add(p.id)
        if not points or offset is None:
            break
    return ids


def store_facts(facts: list[dict], source: str, mode="replace") -> int:
    if not facts:
        print(f"  No facts for source={source}")
        return 0

    # Secret Guard — redact secrets in all facts before storing
    for f in facts:
        f["text"] = scrub(f.get("text", ""), source)

    if mode == "replace":
        clear_source(source)
        planned = [(f, point_id(source, f["text"])) for f in facts]
    else:
        # append: embed and upload ONLY new facts (content-based ID = natural dedup)
        existing = get_existing_ids(source)
        planned = []
        for f in facts:
            pid = point_id(source, f["text"])
            if pid not in existing:
                planned.append((f, pid))
        if not planned:
            print(
                f"  {len(facts)} facts already in Qdrant — nothing new for source={source}"
            )
            return 0
        print(f"  New facts: {len(planned)}/{len(facts)} for source={source}")

    model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    total = len(planned)
    stored = 0
    for bs in range(0, total, _BATCH_SIZE):
        batch = planned[bs : bs + _BATCH_SIZE]
        texts = [f[0]["text"] for f in batch]
        vecs = list(model.embed(texts))
        points = []
        for i, (fact, pid) in enumerate(batch):
            vec = vecs[i].tolist()
            # ts_epoch = content date; time-feature vector consistent with ts_epoch
            cts = content_ts(fact)
            payload = {
                "text": fact["text"],
                "source": source,
                "section": fact.get("section", ""),
                "file_path": fact.get("file_path", ""),
                "type": fact.get("type", ""),
            }
            if COLLECTION.endswith("-v2"):
                vec += time_features(cts)
                payload["ts_epoch"] = cts
            points.append(PointStruct(id=pid, vector=vec, payload=payload))
        client.upsert(collection_name=COLLECTION, points=points)
        stored += len(batch)
        print(f"  batch {bs // _BATCH_SIZE + 1}: {stored}/{total}")
        gc.collect()
    print(f"  Stored {stored} facts, source={source}")
    return stored


# ===== SOURCES =====


def ingest_vps():
    facts = []
    sec = ""
    with open(VPS_DOC_PATH) as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("## "):
                sec = line.lstrip("# ").strip()
                continue
            if line.startswith("### "):
                sec = line.lstrip("# ").strip()
                continue
            if line.startswith("| **"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 4:
                    name = parts[0].replace("**", "")
                    rest = " | ".join(parts[1:])
                    facts.append(
                        {
                            "text": f"{sec}: {name} — {rest}"[:800],
                            "section": sec,
                            "file_path": VPS_DOC_PATH,
                            "type": "service",
                        }
                    )
                continue
            if line.startswith("```") or line.startswith("    "):
                continue
            if line.startswith("- ") or line.startswith("* "):
                facts.append(
                    {
                        "text": f"{sec}: {line.lstrip('-* ')}"[:800],
                        "section": sec,
                        "file_path": VPS_DOC_PATH,
                        "type": "config",
                    }
                )
                continue
            if len(line) > 40 and not line.startswith("|"):
                facts.append(
                    {
                        "text": f"{sec}: {line}"[:800],
                        "section": sec,
                        "file_path": VPS_DOC_PATH,
                        "type": "documentation",
                    }
                )
    store_facts(facts, source="vps-docs", mode="replace")
    return len(facts)


def ingest_changelog():
    # By default incremental (append) — embeddings only for new entries.
    # Full re-ingest: ingest.py changelog --replace
    mode = "replace" if FORCE_REPLACE else "append"
    facts = []
    date = ""
    with open(CHANGELOG_PATH) as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            m = re.match(r"^## (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?) — (.+)$", line)
            if m:
                date = m.group(1)
                facts.append(
                    {
                        "text": f"{date}: {m.group(2)}",
                        "section": "changelog",
                        "file_path": CHANGELOG_PATH,
                        "type": "changelog-entry",
                        "date": date,
                    }
                )
                continue
            if line.startswith("- ") and date:
                facts.append(
                    {
                        "text": f"{date}: {line.lstrip('- ')}"[:800],
                        "section": "changelog",
                        "file_path": CHANGELOG_PATH,
                        "type": "changelog-detail",
                        "date": date,
                    }
                )
    store_facts(facts, source="changelog", mode=mode)
    return len(facts)


def ingest_instructions():
    import glob

    facts = []
    proj = {}
    for f in glob.glob(os.path.join(WWW_ROOT, "**", "INSTRUKCJA*"), recursive=True):
        if "node_modules" not in f and ".git" not in f:
            pn = f.split("/var/www/")[1].split("/")[0]
            proj.setdefault(pn, []).append(f)
    for f in glob.glob(os.path.join(WWW_ROOT, "**", "README.md"), recursive=True):
        if "node_modules" not in f and ".git" not in f:
            pn = f.split("/var/www/")[1].split("/")[0]
            if f not in proj.get(pn, []):
                proj.setdefault(pn, []).append(f)
    for project, files in proj.items():
        src = f"project-{project}"
        pf = []
        for fp in files:
            try:
                with open(fp) as fh:
                    c = fh.read()
                for p in c.split("\n\n"):
                    p = p.strip()
                    if (
                        len(p) > 30
                        and not p.startswith("#")
                        and not p.startswith("```")
                    ):
                        pf.append(
                            {
                                "text": f"[{project}] {p.replace(chr(10), ' ')}"[:800],
                                "section": project,
                                "file_path": fp,
                                "type": "project-doc",
                            }
                        )
            except Exception as e:
                print(f"  Skipped {fp}: {e}")
        if pf:
            clear_source(src)
            store_facts(pf, source=src, mode="append")
            facts.extend(pf)
            print(f"  project {project}: {len(pf)} facts")
    return len(facts)


def ingest_nginx():
    import glob

    facts = []
    for fp in glob.glob(os.path.join(NGINX_DIR, "*")):
        if "default" in fp:
            continue
        try:
            with open(fp) as f:
                c = f.read()
            sn = re.findall(r"server_name\s+([^;]+);", c)
            pp = re.findall(r"proxy_pass\s+(https?://[^;]+);", c)
            li = re.findall(r"listen\s+([^;]+);", c)
            rl = re.findall(r"rate=([^;]+);", c)
            name = sn[0].strip() if sn else os.path.basename(fp)
            for port in (l.strip() for l in li):
                for proxy in pp:
                    t = f"nginx: {name} → {proxy} (port {port})"
                    if rl:
                        t += f" | rate limit: {rl[0]}"
                    facts.append(
                        {
                            "text": t[:400],
                            "section": "nginx-routes",
                            "file_path": fp,
                            "type": "nginx-route",
                        }
                    )
        except Exception as e:
            print(f"  Skipped {fp}: {e}")
    store_facts(facts, source="nginx-routes", mode="replace")
    return len(facts)


def ingest_systemd():
    import glob

    facts = []
    for fp in glob.glob(os.path.join(SYSTEMD_DIR, "*.service")):
        try:
            with open(fp) as f:
                c = f.read()
            desc = re.search(r"Description=([^\n]+)", c)
            ex = re.search(r"ExecStart=([^\n]+)", c)
            if ex:
                name = os.path.basename(fp).replace(".service", "")
                t = f"systemd service {name}: {ex.group(1).strip()}"
                if desc:
                    t += f" ({desc.group(1).strip()})"
                facts.append(
                    {
                        "text": t[:400],
                        "section": "systemd-services",
                        "file_path": fp,
                        "type": "systemd",
                    }
                )
        except Exception as e:
            print(f"  Skipped {fp}: {e}")
    store_facts(facts, source="systemd-services", mode="replace")
    return len(facts)


def ingest_infrastructure():
    # Sample static facts (teach your own!). Write your own, and they will be
    # ingested with source="infrastructure". Remember: secret_guard.py redacts secrets.
    facts = [
        {
            "text": "Example fact: disk usage is checked before/after installs with df -h /.",
            "section": "infrastructure",
            "file_path": "rules",
            "type": "infrastructure",
        },
        {
            "text": "Example fact: after every deploy, append to the project CHANGELOG.md.",
            "section": "infrastructure",
            "file_path": "rules",
            "type": "infrastructure",
        },
    ]
    store_facts(facts, source="infrastructure", mode="replace")
    return len(facts)


SOURCES = {
    "vps-docs": ingest_vps,
    "changelog": ingest_changelog,
    "instructions": ingest_instructions,
    "nginx": ingest_nginx,
    "systemd": ingest_systemd,
    "infrastructure": ingest_infrastructure,
}


def main():
    # Sequential mode: each source as a separate process → RAM-safe
    if len(sys.argv) > 1 and sys.argv[1] == "--sequential":
        total = 0
        for name in SOURCES:
            print(f"\n=== INGEST: {name} (separate process) ===\n")
            cmd = [sys.executable, __file__, name]
            if FORCE_REPLACE:
                cmd.append("--replace")
            r = subprocess.run(cmd, capture_output=False)
            if r.returncode != 0:
                print(f"  ❌ {name} FAILED (exit code {r.returncode})")
            else:
                total += 1
        print(
            f"\n✅ Sequential ingest finished: {total}/{len(SOURCES)} sources ready"
        )
        return

    # Mode with --only (single source); --replace is parsed globally
    args = [a for a in sys.argv[1:] if a != "--replace"]
    only = args[0] if args else None
    if only:
        if only not in SOURCES:
            print(f"Unknown: {only}. Available: {', '.join(SOURCES.keys())}")
            sys.exit(1)
        print(f"=== Ingest: {only} ===\n")
        n = SOURCES[only]()
        print(f"\n✅ {n} facts from {only}")
        return
    total = 0
    print("=== Full Qdrant ingest ===\n")
    for name, fn in SOURCES.items():
        print(f"[{name}]")
        n = fn()
        total += n
        print(f"      {n} facts\n")
    print(f"✅ Ingest finished: {total} facts")


if __name__ == "__main__":
    main()
