#!/usr/bin/env python3
"""Generate counted documentation facts directly from source without importing services."""
import ast
import json
from pathlib import Path
import plistlib
import re
import tomllib
ROOT=Path(__file__).resolve().parents[2]


def snapshot(root=ROOT):
    config=tomllib.loads((root/'config/backends.toml').read_text())
    tree=ast.parse((root/'src/backends/resolver.py').read_text())
    adapters=[]
    for node in tree.body:
        if isinstance(node,ast.AnnAssign) and getattr(node.target,'id',None)=='DEFAULT_ADAPTERS':
            adapters=[ast.literal_eval(key) for key in node.value.keys]
    schedules=[]
    for p in sorted((root/'scheduling').glob('*.plist')):
        doc=plistlib.loads(p.read_bytes())
        schedules.append({'file':p.name,'label':doc.get('Label'), 'calendar':doc.get('StartCalendarInterval'),
                          'interval_seconds':doc.get('StartInterval'), 'keep_alive':doc.get('KeepAlive',False),
                          'arguments':doc.get('ProgramArguments',[])})
    roles=sorted(config['profiles'][config['default_profile']])
    cap=re.search(r'SECOND_BRAIN_RUN_COST_CAP_USD:-([0-9.]+)',(root/'scripts/jobs/dream_cycle_scheduled.sh').read_text())
    base=re.search(r'DEFAULT_RUN_COST_CAP_USD = ([0-9.]+)',(root/'src/backends/cost.py').read_text())
    return {'adapters':sorted(adapters),'default_profile':config['default_profile'],'profiles':sorted(config['profiles']),
            'roles':roles,'migration_high_water':max(p.name for p in (root/'migrations').glob('[0-9]*.sql')),
            'scheduled_spend_threshold_usd':float(cap.group(1)) if cap else None,
            'library_spend_threshold_usd':float(base.group(1)),'schedules':schedules}


def markdown(facts):
    lines=['# Generated runtime inventory','', 'Source-derived configuration, not installed or healthy service state.', '',
           f"Adapters: {len(facts['adapters'])}. Profiles: {len(facts['profiles'])}. Roles per profile: {len(facts['roles'])}. Schedule templates: {len(facts['schedules'])}.",
           f"Default profile: `{facts['default_profile']}`. Migration high-water: `{facts['migration_high_water']}`.",
           f"Scheduled spend threshold: ${facts['scheduled_spend_threshold_usd']:g}; library default: ${facts['library_spend_threshold_usd']:g}. Both stop after a metered response and can overshoot.", '',
           '| Schedule | Calendar (launchd local time) / interval | KeepAlive |','|---|---|---|']
    for s in facts['schedules']:
        cadence=json.dumps(s['calendar'],sort_keys=True) if s['calendar'] else (str(s['interval_seconds'])+' seconds' if s['interval_seconds'] else 'on demand / RunAtLoad')
        lines.append(f"| `{s['label']}` | {cadence} | {str(s['keep_alive']).lower()} |")
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    data=snapshot(); target=ROOT/'docs/generated'; target.mkdir(exist_ok=True)
    (target/'runtime-inventory.json').write_text(json.dumps(data,indent=2)+'\n')
    (target/'runtime-inventory.md').write_text(markdown(data))
