"""Blind human-audit kit for the 40 J1 borderline items (no model call).
  make:  python -m pilots.j1_human_audit make ITEMS.json OUT_DIR   -> OUT_DIR/audit_sheet.html (self-contained, no verdict shown) and OUT_DIR/audit_sheet_key.json
         (opaque id -> item_id; kept apart from the sheet). Item order is shuffled by the recorded seed SEED.
  score: python -m pilots.j1_human_audit score KEY.json HUMAN.json JUDGE.jsonl AUDIT.jsonl BARS.json OUT.json
         HUMAN.json = {"a01": true|false, ...} for all 40 (the sheet's Download button writes it). Withholds if any answer is missing or not boolean.
The sheet shows exactly the grading instructions, question, correct answer and response that the judge saw. It carries no judge verdict, no Codex
verdict and no item kind.
"""
import hashlib
import html
import json
import os
import random
import sys

from . import j1_items as J
from .j1_score import wilson

SEED = "16096:J1:humanaudit"


def make(items_path, out_dir):
    items = [i for i in json.load(open(items_path)) if i["category"] == "borderline"]
    assert len(items) == 40
    order = sorted(items, key=lambda i: i["item_id"])
    random.Random(SEED).shuffle(order)
    key, cards = {}, []
    for n, i in enumerate(order, 1):
        oid = "a%02d" % n
        key[oid] = i["item_id"]
        cards.append('<section class="card" data-id="%s"><h2>Item %d of 40</h2><pre>%s</pre>'
                     '<label><input type="radio" name="%s" value="yes"> yes (the response is correct under these instructions)</label> '
                     '<label><input type="radio" name="%s" value="no"> no</label></section>' % (oid, n, html.escape(J.audit_prompt(i)), oid, oid))
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>J1 blind audit sheet</title><style>
:root{--bg:#fff;--fg:#1b1b1b;--card:#f4f4f6;--line:#c9c9d1}@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececf1;--card:#212127;--line:#3a3a44}}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:0 auto;padding:1rem 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:16px 0}pre{white-space:pre-wrap;word-wrap:break-word;font:inherit}
label{margin-right:1.5rem}button{font:inherit;padding:8px 16px}</style></head><body>
<h1>J1 blind audit sheet (40 items)</h1>
<p>For each item apply the grading instructions exactly as written and answer yes or no. No grader verdict is shown. When all 40 are answered, press Download and send the file back.</p>
%s
<p><button id="dl">Download answers</button> <span id="msg"></span></p>
<script>
document.getElementById('dl').onclick=function(){var out={},miss=0;document.querySelectorAll('.card').forEach(function(c){
var id=c.getAttribute('data-id'),v=c.querySelector('input:checked');if(v){out[id]=(v.value==='yes');}else{miss++;}});
if(miss){document.getElementById('msg').textContent=miss+' item(s) unanswered';return;}
var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:'application/json'}));a.download='human_audit_answers.json';a.click();
document.getElementById('msg').textContent='downloaded';};
</script></body></html>""" % "\n".join(cards)
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "audit_sheet.html"), "w", encoding="utf-8").write(page)
    json.dump({"seed": SEED, "key": key, "sheet_sha256": hashlib.sha256(page.encode()).hexdigest()}, open(os.path.join(out_dir, "audit_sheet_key.json"), "w"), indent=1)
    print("wrote sheet for", len(key), "items")


def score(key_p, human_p, judge_p, audit_p, bars_p, out_p):
    key = json.load(open(key_p))["key"]
    human = json.load(open(human_p))
    bars = json.load(open(bars_p))
    problems = [k for k in key if not isinstance(human.get(k), bool)] + ["unknown id %s" % k for k in human if k not in key]
    if problems:
        res = {"status": "WITHHELD", "problems": problems}
    else:
        jr = {r["item_id"]: r["verdict"] for r in map(json.loads, open(judge_p)) if r}
        cx = {r["item_id"]: r["verdict"] for r in map(json.loads, open(audit_p)) if r}
        agree = sum(1 for k, iid in key.items() if human[k] == jr[iid])
        hx = sum(1 for k, iid in key.items() if human[k] == cx[iid])
        lo = wilson(agree, 40)[0]
        res = {"status": "SCORED", "label": "human audit by the user", "judge_vs_human": {"k": agree, "n": 40, "rate": agree / 40, "wilson_lower": lo,
               "bar_lower_at_least": bars["agree_lower_min"], "pass": lo >= bars["agree_lower_min"]}, "codex_vs_human": {"k": hx, "n": 40}}
    json.dump(res, open(out_p, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"make": lambda a: make(*a[:2]), "score": lambda a: score(*a[:6])}[sys.argv[1]](sys.argv[2:])
