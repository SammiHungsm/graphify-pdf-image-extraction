# Patching graph.json after a query miss

When `graphify query` returns no relevant nodes because an entity was not
extracted into the graph, you can patch `graph.json` directly instead of
re-running the full pipeline. This document captures the recipe and the
naming conventions to follow so the patched graph stays consistent with
auto-extracted nodes.

## When to patch vs rebuild

| Situation                                        | Action                            |
| ------------------------------------------------ | --------------------------------- |
| 1–3 missing entities, source file is known       | Patch (this file)                 |
| Many missing entities, full document under-extracted | `graphify update` or full re-run |
| Graph is structurally broken (wrong communities, etc.) | Re-run from clustering     |

A full re-run is overkill for a handful of missing facts — the targeted JSON
edit + `graphify export html` is roughly 100× faster and costs zero LLM tokens.

## graph.json shape (the bits you need)

```json
{
  "nodes": [
    {
      "id": "ar2023_ck_hutchison_holdings",
      "label": "CK Hutchison Holdings Limited",
      "norm_label": "ck hutchison holdings limited",
      "file_type": "entity",
      "source_file": "ar2023.pdf",
      "source_location": null,
      "community": 16
    }
  ],
  "links": [
    {
      "source": "ar2023_tut1",
      "target": "ar2023_ut1",
      "relation": "trustee_of",
      "confidence": "EXTRACTED",
      "confidence_score": 1.0,
      "source_file": "ar2023.pdf",
      "source_location": "p.94",
      "weight": 1.0
    }
  ]
}
```

Field rules:

- `id` — `snake_case`, unique across the file. Use `<source_stem>_<slug>`.
- `label` — display string the visualizer shows.
- `norm_label` — lowercase copy of `label` (used for matching).
- `file_type` — `"entity"` for manual additions; `"paper"` / `"document"` /
  `"concept"` for extracted nodes.
- `source_file` / `source_location` — always set. `source_location` is the
  page or section, e.g. `"p.94"`.
- `community` — copy from a related existing node; do not invent a new one.
- `relation` — `snake_case` verb (`trustee_of`, not `Trustee Of`).

## Locate the source first

Identify the file and page (or section) that contains the missing fact. Read
it directly — do not trust the graph for the answer. Use whatever fits the
file type:

```python
import fitz; doc = fitz.open("raw/foo.pdf"); doc[93].get_text()  # PDF
Path("raw/foo.md").read_text(encoding="utf-8")                   # MD / TXT
json.load(open("raw/foo.json"))                                  # JSON
```

## Worked example: CK Hutchison trust structure

The query *"What is the percentage of shareholding of Li Ka-Shing Unity
Trustee Company Limited (TUT1) as trustee of The Li Ka-Shing Unity Trust
(UT1)? (p 94)"* returned BFS hits from community 0 (China Resources
Pharmaceutical) — completely unrelated. The graph already had
`ar2023_ck_hutchison_holdings` in community 16, but no trust / trustee /
Li Ka-shing nodes. The annual report's SFO disclosure table on p.94 was
flattened and skipped during extraction.

### 1. Find the source

```python
import fitz
from pathlib import Path

for pdf in sorted(Path("raw").glob("*.pdf")):
    doc = fitz.open(pdf)
    for i, page in enumerate(doc, 1):
        if "TUT1" in page.get_text() or "Li Ka-Shing Unity" in page.get_text():
            print(f"{pdf.name} p.{i}")
    doc.close()
# -> ar2023.pdf p.82 (table starts), key row on p.94
```

Open the page, read the text, and answer the user's question. Quote the
page number.

### 2. Back up the graph

```bash
cp graphify-out/graph.json graphify-out/graph.json.bak
```

Patching is destructive. Reverting from the backup is cheaper than rebuilding
from scratch.

### 3. Patch graph.json

```python
import json
from pathlib import Path

g = json.loads(Path("graphify-out/graph.json").read_text(encoding="utf-8"))

# Dedupe — never silently double-insert
assert "ar2023_tut1" not in {n["id"] for n in g["nodes"]}
ck_id = "ar2023_ck_hutchison_holdings"
assert ck_id in {n["id"] for n in g["nodes"]}

src, loc = "ar2023.pdf", "p.94"

g["nodes"].extend([
    {"id": "ar2023_tut1",
     "label": "Li Ka-Shing Unity Trustee Company Limited (TUT1)",
     "norm_label": "li ka-shing unity trustee company limited (tut1)",
     "file_type": "entity", "source_file": src, "source_location": loc,
     "community": 16},
    {"id": "ar2023_ut1",
     "label": "The Li Ka-Shing Unity Trust (UT1)",
     "norm_label": "the li ka-shing unity trust (ut1)",
     "file_type": "entity", "source_file": src, "source_location": loc,
     "community": 16},
    # ... TDT1, DT1, TDT2, DT2, Li Ka-shing ...
])

def link(s, t, r):
    return {"source": s, "target": t, "relation": r,
            "confidence": "EXTRACTED", "confidence_score": 1.0,
            "source_file": src, "source_location": loc, "weight": 1.0}

g["links"].extend([
    link("ar2023_tut1", "ar2023_ut1", "trustee_of"),
    link("ar2023_tut1", ck_id, "holds_26.26_percent"),
    # ...
])

Path("graphify-out/graph.json").write_text(
    json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8"
)
```

### 4. Verify and re-export

Re-run the original query — the new nodes should appear with `loc=p.94`
populated:

```bash
graphify query "TUT1 shareholding percentage" --budget 600
```

The visualizer (`graph.html`) does **not** auto-update when `graph.json`
changes. Regenerate it explicitly:

```bash
graphify export html
```

## Naming conventions

Bake values into the relation name when you can — it makes the graph
self-documenting and saves a follow-up query.

| Entity class                            | Convention                                       |
| --------------------------------------- | ------------------------------------------------ |
| Existing extracted node                 | `<source_stem>_<snake_case_label>`               |
| Manually-added entity                   | Same prefix + manual suffix                      |
| Trust relationship                      | `trustee_of` (source=trustee, target=trust)      |
| Founder relationship                    | `founder_of` (source=person, target=trust)       |
| Shareholding with known percentage      | `holds_<NN.NN>_percent`                          |
| Generic substantial shareholder         | `substantial_shareholder_<NN.NN>_percent`        |

## What the patched graph does NOT update

- `cost.json` token totals — manual additions are free, do not bill for them.
- `GRAPH_REPORT.md` — regenerate via a full re-run if you want the new
  entities in the report.
- `graph.html` — regenerate explicitly with `graphify export html`.
- `obsidian/` — per-node files; only on `--obsidian` runs.
