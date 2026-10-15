"""One-time patch for the existing WSL Fantasy parser.

Usage: python install_wsl_fixture_sync.py path/to/YOUR_PARSER.py
Place wsl_fixture_schedule_sync.py alongside YOUR_PARSER.py first.
Makes a .fixture_sync_backup copy before editing.
"""
from pathlib import Path
import shutil
import sys

if len(sys.argv) != 2:
    raise SystemExit("Usage: python install_wsl_fixture_sync.py path/to/your_parser.py")
p = Path(sys.argv[1]).resolve()
s = p.read_text(encoding='utf-8')
if 'from wsl_fixture_schedule_sync import' in s:
    raise SystemExit('Fixture sync already installed; no changes made.')
if not (p.parent/'wsl_fixture_schedule_sync.py').exists():
    raise SystemExit('Put wsl_fixture_schedule_sync.py in the same folder as the parser first.')
changes = [
    ('import requests\n',
     'import requests\n\nfrom wsl_fixture_schedule_sync import (\n'
     '    fetch_website_matches, reconcile_fantasy_fixture_feed,\n)\n'),
    ('    fixtures_raw = value_of(feeds["fixtures"]) or []\n',
     '    fixtures_raw = value_of(feeds["fixtures"]) or []\n'
     '    if from_local:\n'
     '        schedule_sync_report = {"status": "skipped_from_local"}\n'
     '    else:\n'
     '        # Reconcile once, before gameweek clustering and all fixture-dependent outputs.\n'
     '        # Do not silently publish stale GK coverage if sync fails.\n'
     '        website_matches = fetch_website_matches()\n'
     '        schedule_sync_report = reconcile_fantasy_fixture_feed(\n'
     '            fixtures_raw, website_matches,\n'
     '        )\n'
     '        schedule_sync_report["status"] = "verified"\n'
     '        print(\n'
     '            f"Fixture schedule sync: {schedule_sync_report[\'corrected\']} "\n'
     '            f"corrections, {schedule_sync_report[\'unmatched_count\']} unmatched, "\n'
     '            f"{schedule_sync_report[\'unknown_kickoff_count\']} unconfirmed."\n'
     '        )\n'),
    ('        "fixture_count": len(fixtures),\n',
     '        "fixture_count": len(fixtures),\n'
     '        "fixture_schedule_sync": schedule_sync_report,\n'),
]
for before, after in changes:
    count=s.count(before)
    if count!=1:
        raise SystemExit(f'Expected exactly one integration anchor, got {count}: {before!r}. No changes made.')
    s=s.replace(before,after,1)
compile(s,str(p),'exec')
backup=p.with_name(p.name+'.fixture_sync_backup')
shutil.copy2(p,backup)
p.write_text(s,encoding='utf-8')
print(f'Patched {p.name} (syntax checked). Backup: {backup.name}')
