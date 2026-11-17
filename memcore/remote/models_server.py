"""Runs on t2-dev with the retrieval owner's venv. Reads {"op": "embed"|"rerank", ...} on stdin, prints one JSON line.
Read-only use of the HF cache in ~/emlab-16096/models (offline mode); writes nothing."""
import json, os, sys
os.environ["HF_HUB_OFFLINE"] = "1"
CACHE = os.path.expanduser("~/emlab-16096/models")
import torch
from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "4")))
op = json.loads(sys.stdin.read())
if op["op"] == "embed":
    name = "intfloat/multilingual-e5-small"
    tok = AutoTokenizer.from_pretrained(name, cache_dir=CACHE)
    model = AutoModel.from_pretrained(name, cache_dir=CACHE).eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(op["texts"]), 16):
            b = tok(op["texts"][i:i + 16], padding=True, truncation=True, max_length=512, return_tensors="pt")
            h = model(**b).last_hidden_state
            m = b["attention_mask"].unsqueeze(-1).float()
            v = (h * m).sum(1) / m.sum(1)
            v = torch.nn.functional.normalize(v, p=2, dim=1)
            out += v.tolist()
    print(json.dumps({"vectors": out}))
else:
    name = "BAAI/bge-reranker-v2-m3"
    tok = AutoTokenizer.from_pretrained(name, cache_dir=CACHE)
    model = AutoModelForSequenceClassification.from_pretrained(name, cache_dir=CACHE).eval()
    sc = []
    with torch.no_grad():
        for d in op["docs"]:
            b = tok([[op["query"], d]], padding=True, truncation=True, max_length=512, return_tensors="pt")
            sc.append(float(model(**b).logits.view(-1)[0]))
    print(json.dumps({"scores": sc}))
