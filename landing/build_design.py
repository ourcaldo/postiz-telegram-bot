"""Merge extracted design constants with worker logic into index.js.

Run from landing/: python build_design.py
Inputs: design_consts.js (generated from instagram-bio_v4.html), logic below.
Output: index.js (self-contained Cloudflare Worker).
"""

consts = open("design_consts.js", encoding="utf-8").read()

LOGIC_FILE_MARKER = None  # logic lives in logic.js.template for maintainability
logic = open("logic.js.template", encoding="utf-8").read()

out = consts + "\n" + logic
open("index.js", "w", encoding="utf-8").write(out)
print("index.js written:", len(out), "bytes")
