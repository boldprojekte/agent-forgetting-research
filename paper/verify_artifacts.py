"""Offline arithmetic and file-integrity checks; does not rerun models or graders."""
from pathlib import Path
import csv
import hashlib
import json

ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads((ROOT/p).read_text())
metrics=read('evidence/otel-checkout-noise-03-pair/metrics.json')
for arm in ['retained','method']:
 for phase,key in [('primary','primary'),('followup','final')]:
  s=read(f'runs/otel-checkout-noise-03-{arm}/{phase}/summary.json');u=s['usage'];m=metrics['arms'][arm][key]
  assert sum(x['prompt_tokens'] for x in u)==m['prompt_tokens_cumulative']
  assert sum(x['completion_tokens'] for x in u)==m['completion_tokens_cumulative']
  assert len(u)==m['requests_cumulative']
  assert u[-1]['prompt_tokens']==m['final_prompt_tokens']
  assert max(x['prompt_tokens'] for x in u)==m['peak_prompt_tokens']
with (ROOT/'paper/figures/otel-prompt-series.csv').open() as f:
 rows=list(csv.DictReader(f))
assert len(rows)==306
for row in rows:
 u=read(f'runs/otel-checkout-noise-03-{row["arm"]}/followup/summary.json')['usage'][int(row['request'])-1]
 assert int(row['prompt_tokens'])==u['prompt_tokens']
frozen=read('evaluation/otel-checkout-noise-03/manifest.json')['files']
for path,sha in frozen.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==sha,path
r=metrics['arms']['retained']['final'];m=metrics['arms']['method']['final']
print(json.dumps({'status':'passed','frozen_files':len(frozen),'usage_rows':len(rows),'final_prompt_reduction_pct':100*(1-m['final_prompt_tokens']/r['final_prompt_tokens']),'input_reduction_pct':100*(1-m['prompt_tokens_cumulative']/r['prompt_tokens_cumulative']),'scope':'Arithmetic and hash consistency only; not a new behavioral evaluation.'},indent=2))
