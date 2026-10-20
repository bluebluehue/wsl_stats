#!/usr/bin/env python3
"""Read cached Opta match JSON and write throw-in CSVs; never modifies input files."""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

SOURCE = Path('raw_opta_events')
OUTPUT = Path('throwin_analysis')
FIELDS = ['date', 'match', 'team', 'opponent', 'player', 'player_id', 'minute', 'second', 'x', 'y', 'half', 'side', 'event_id']


def write_csv(path, rows, columns):
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not SOURCE.is_dir():
        raise SystemExit(f'Missing {SOURCE}/. Run from the repository root.')
    OUTPUT.mkdir(exist_ok=True)
    all_events = []
    seen = set()
    matched = 0
    invalid = 0
    for path in sorted(SOURCE.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            info = data['matchInfo']
            events = data['liveData']['event']
        except (ValueError, KeyError, TypeError, OSError):
            invalid += 1
            continue
        mid = str(info.get('id') or path.stem)
        if mid in seen:
            continue
        seen.add(mid)
        matched += 1
        teams = {str(t.get('id')): t.get('name', '') for t in info.get('contestant', [])}
        for e in events:
            if e.get('typeId') != 1 or not any(q.get('qualifierId') == 107 for q in (e.get('qualifier') or [])):
                continue
            team = teams.get(str(e.get('contestantId')), '')
            opponent = next((n for k, n in teams.items() if k != str(e.get('contestantId'))), '')
            try:
                x, y = float(e['x']), float(e['y'])
            except (ValueError, TypeError, KeyError):
                continue
            all_events.append({
                'date': info.get('localDate', info.get('date', '')),
                'match': info.get('description', ''), 'team': team, 'opponent': opponent,
                'player': e.get('playerName', ''), 'player_id': e.get('playerId', ''),
                'minute': e.get('timeMin', ''), 'second': e.get('timeSec', ''),
                'x': x, 'y': y,
                'half': 'attacking' if x > 50 else ('defensive' if x < 50 else 'halfway'),
                'side': 'touchline 0' if y <= 5 else ('touchline 100' if y >= 95 else 'other'),
                'event_id': e.get('id', ''),
            })
    all_events.sort(key=lambda r: (r['date'], r['match'], int(r['minute'] or 0), int(r['second'] or 0)))
    write_csv(OUTPUT / 'all_throwins.csv', all_events, FIELDS)

    groups = defaultdict(list)
    match_groups = defaultdict(list)
    for e in all_events:
        groups[(e['team'], e['player_id'], e['player'])].append(e)
        match_groups[(e['date'], e['match'], e['team'], e['player_id'], e['player'])].append(e)
    def count_row(events):
        c = Counter(e['half'] for e in events)
        return {'throws': len(events), 'attacking': c['attacking'], 'defensive': c['defensive'], 'halfway': c['halfway'],
                'attacking_pct': round(100 * c['attacking'] / len(events), 1)}
    summary = [dict(team=t, player=p, player_id=pid, **count_row(es)) for (t,pid,p),es in groups.items()]
    summary.sort(key=lambda r: (-r['throws'], r['team'], r['player']))
    write_csv(OUTPUT / 'player_totals.csv', summary, ['team', 'player', 'player_id', 'throws', 'attacking', 'defensive', 'halfway', 'attacking_pct'])
    per_match = [dict(date=d, match=m, team=t, player=p, player_id=pid, **count_row(es)) for (d,m,t,pid,p),es in match_groups.items()]
    per_match.sort(key=lambda r: (r['date'], r['match'], -r['throws']))
    write_csv(OUTPUT / 'player_by_match.csv', per_match, ['date', 'match', 'team', 'player', 'player_id', 'throws', 'attacking', 'defensive', 'halfway', 'attacking_pct'])
    chelsea = [r for r in all_events if 'chelsea' in r['team'].lower()]
    write_csv(OUTPUT / 'chelsea_throwins.csv', chelsea, FIELDS)
    print(f'Processed {matched} unique match files ({invalid} unreadable/skipped).')
    print(f'Found {len(all_events)} throw-ins, including {len(chelsea)} Chelsea throw-ins.')
    print('Saved four CSV files in throwin_analysis/.')
    print('\nChelsea players:')
    for r in summary:
        if 'chelsea' in r['team'].lower():
            print(f"  {r['player']}: {r['throws']} total, {r['attacking']} attacking-half")


if __name__ == '__main__':
    main()
