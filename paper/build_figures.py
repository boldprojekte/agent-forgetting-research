# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.10.8"]
# ///
"""Rebuild paper figures from recorded provider usage; no inference or grading."""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'paper' / 'figures'
OUT.mkdir(exist_ok=True)
COLORS = {'retained': '#5C6675', 'method': '#007F73'}
plt.rcParams.update({'font.size': 10, 'axes.spines.top': False,
                     'axes.spines.right': False, 'svg.fonttype': 'none'})
fig, axes = plt.subplots(1, 2, figsize=(11, 4), constrained_layout=True)
rows = []
for arm in ('retained', 'method'):
    root = ROOT / 'runs' / f'otel-checkout-noise-03-{arm}'
    primary = json.loads((root/'primary/summary.json').read_text())
    final = json.loads((root/'followup/summary.json').read_text())
    boundary = len(primary['usage'])
    for i, usage in enumerate(final['usage'], 1):
        rows.append({'arm': arm, 'request': i, 'phase': 'primary' if i <= boundary else 'followup',
                     'prompt_tokens': usage['prompt_tokens'], 'completion_tokens': usage['completion_tokens']})
    y = [u['prompt_tokens']/1000 for u in final['usage']]
    axes[0].plot(range(1,len(y)+1),y,label=arm.title(),color=COLORS[arm],linewidth=1.8)
    axes[0].scatter([boundary],[y[boundary-1]],color=COLORS[arm],marker='D',s=40,zorder=3)
    axes[0].annotate(f'{y[-1]:,.0f}k',(len(y), y[-1]),xytext=(-5,8),textcoords='offset points',ha='right',color=COLORS[arm])
axes[0].set(title='A. OpenTelemetry: observed prompt size',xlabel='Model request within each session',ylabel='Provider-reported prompt tokens (thousands)',ylim=(0,1020))
axes[0].legend(loc='upper left')
axes[0].grid(axis='y',alpha=.15)
plan = json.loads((ROOT/'evidence/planroom-v12-pair/metrics.json').read_text())
otel = json.loads((ROOT/'evidence/otel-checkout-noise-03-pair/metrics.json').read_text())
labels=['OTel\nprimary end','OTel\nfollow-up end','Planroom\nend']
for index,arm in enumerate(('retained','method')):
    a=otel['arms'][arm]
    vals=[a['primary']['final_prompt_tokens']/1000,a['final']['final_prompt_tokens']/1000,plan[arm]['final_prompt_tokens']/1000]
    bars=axes[1].bar([i+(index-.5)*.34 for i in range(3)],vals,width=.34,color=COLORS[arm],label=arm.title())
    axes[1].bar_label(bars,labels=[f'{v:.0f}k' for v in vals],fontsize=9,padding=3)
axes[1].set(title='B. Endpoints across contrasting tasks',ylabel='Provider-reported prompt tokens (thousands)',xticks=range(3),xticklabels=labels,ylim=(0,1020))
axes[1].grid(axis='y',alpha=.15)
fig.suptitle('Reversible curation reduces context in the noisy case, but not in every task',fontsize=12)
for extension in ('png','pdf','svg'):
    fig.savefig(OUT/f'context-results.{extension}',dpi=180)
with (OUT/'otel-prompt-series.csv').open('w',newline='') as stream:
    writer=csv.DictWriter(stream,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)
print(f'Wrote figure and {len(rows)} usage rows to {OUT}')
