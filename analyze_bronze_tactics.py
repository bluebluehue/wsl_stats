#!/usr/bin/env python3
"""Read-only Opta F24 analysis: Chelsea throw-ins, on-pitch teammates and subsequent actions.
No third-party dependencies. Does not modify raw data or fantasy calculation files.
"""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

SOURCE = Path('raw_opta_events')
OUT = Path('bronze_tactical_analysis')


def write(name, rows, fields):
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def qual(e, id):
    return next((q.get('value', True) for q in (e.get('qualifier') or []) if q.get('qualifierId') == id), None)


def seconds(e):
    return 60 * int(e.get('timeMin') or 0) + int(e.get('timeSec') or 0)


def name(e):
    return str(e.get('playerName') or e.get('player') or '')


def is_bronze(e):
    return 'bronze' in name(e).casefold()


def is_carpenter(e):
    return 'carpenter' in name(e).casefold()


def active_intervals(events, team_id, player_id, duration):
    # In F24, type 34 = initial formation; qualifier 30 is comma-separated player IDs
    # and qualifier 131 marks 1..11 starters, 0 substitutes.
    formations = [e for e in events if e.get('contestantId') == team_id and e.get('typeId') == 34]
    starter = None
    if formations:
        f = formations[0]
        ids = [x.strip() for x in str(qual(f, 30) or '').split(',')]
        positions = [x.strip() for x in str(qual(f, 131) or '').split(',')]
        starter = any(pid == player_id and idx < len(positions) and positions[idx] not in ('0', '')
                      for idx, pid in enumerate(ids))
    ins = sorted(seconds(e) for e in events if e.get('typeId') == 19 and e.get('playerId') == player_id and e.get('contestantId') == team_id)
    outs = sorted(seconds(e) for e in events if e.get('typeId') == 18 and e.get('playerId') == player_id and e.get('contestantId') == team_id)
    if starter is None and not ins and not outs:
        return [], 'unavailable'
    if starter is None:
        starter = bool(outs)
    changes = [(0, bool(starter))] + [(v, True) for v in ins] + [(v, False) for v in outs]
    changes.sort(key=lambda x: (x[0], x[1]))  # OFF before ON at same second
    spans = []
    start = None
    for t, on in changes:
        if on and start is None:
            start = t
        if not on and start is not None:
            spans.append((start, min(t, duration)))
            start = None
    if start is not None:
        spans.append((start, duration))
    return spans, 'verified' if formations else 'inferred_from_substitutions'


def on_at(spans, t):
    return any(a <= t < b for a, b in spans)


def overlap(a, b):
    return sum(max(0, min(y, v) - max(x, u)) for x, y in a for u, v in b)


def pitch_svg(path, points, label):
    # Plot Chelsea attacking left -> right; Opta x is attacking progression (0..100).
    width, height = 920, 600
    x0, y0, pw, ph = 55, 90, 810, 460
    def xy(x,y):
        return (x0 + x * pw/100, y0 + (100-y)*ph/100)
    strokes = [f'<rect x="{x0}" y="{y0}" width="{pw}" height="{ph}" fill="#133f34" stroke="white" stroke-width="3"/>',
      f'<line x1="{x0+pw/2}" y1="{y0}" x2="{x0+pw/2}" y2="{y0+ph}" stroke="white" stroke-width="2"/>',
      f'<circle cx="{x0+pw/2}" cy="{y0+ph/2}" r="64" fill="none" stroke="white" stroke-width="2"/>',
      f'<rect x="{x0}" y="{y0+115}" width="120" height="230" fill="none" stroke="white" stroke-width="2"/>',
      f'<rect x="{x0+pw-120}" y="{y0+115}" width="120" height="230" fill="none" stroke="white" stroke-width="2"/>']
    for p in points:
        try: x,y=float(p['x']),float(p['y'])
        except (ValueError,TypeError,KeyError): continue
        cx,cy=xy(max(0,min(100,x)),max(0,min(100,y)))
        color='#ffc04d' if x > 50 else '#74d1f6'
        strokes.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6.5" fill="{color}" fill-opacity=".70" stroke="#ffffff" stroke-width=".7"/>')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#112c26"/>
<text x="55" y="44" font-family="Arial" font-size="23" font-weight="bold" fill="white">{label}</text>
<text x="55" y="70" font-family="Arial" font-size="14" fill="#d3e6dd">Blue: defensive half  |  Gold: attacking half  |  Goal attacked →  |  {len(points)} events</text>
{''.join(strokes)}</svg>'''
    path.write_text(svg,encoding='utf-8')


def main():
    if not SOURCE.is_dir(): raise SystemExit('Run from repository root; raw_opta_events/ not found.')
    OUT.mkdir(exist_ok=True)
    seen=set(); throw_rows=[]; next_rows=[]; shared_rows=[]; open_rows=[]; match_rows=[]; maps=defaultdict(list)
    invalid=0; processed=0
    for path in sorted(SOURCE.glob('*.json')):
        try:
            d=json.loads(path.read_text(encoding='utf-8-sig'))
            info=d['matchInfo']; ev=d['liveData']['event']
        except (ValueError,KeyError,OSError,TypeError): invalid+=1;continue
        mid=str(info.get('id') or path.stem)
        if mid in seen:continue
        seen.add(mid); processed+=1
        teams={str(t.get('id')):t.get('name','') for t in info.get('contestant',[])}
        chelsea_id=next((k for k,v in teams.items() if 'chelsea' in v.casefold()),None)
        if not chelsea_id:continue
        date=info.get('localDate',info.get('date',''));match=info.get('description','')
        ev=sorted(ev,key=lambda e:(seconds(e),int(e.get('id') or 0)))
        max_time=max((seconds(e) for e in ev),default=5400)+1
        bronze_id=next((e.get('playerId') for e in ev if is_bronze(e) and e.get('playerId')),None)
        carpenter_id=next((e.get('playerId') for e in ev if is_carpenter(e) and e.get('playerId')),None)
        bronze_sp, bronze_status=active_intervals(ev,chelsea_id,bronze_id,max_time) if bronze_id else ([], 'unavailable')
        carpenter_sp,carpenter_status=active_intervals(ev,chelsea_id,carpenter_id,max_time) if carpenter_id else ([], 'unavailable')
        shared=overlap(bronze_sp,carpenter_sp)
        # Check substitution windows at the instant of the throw, not simply both appearing on teamsheets.
        match_throws=[];count_both=Counter()
        for ix,e in enumerate(ev):
            if e.get('contestantId') != chelsea_id or e.get('typeId') != 1 or qual(e,107) is None:continue
            t=seconds(e); x=e.get('x');y=e.get('y'); player=name(e)
            is_b=is_bronze(e);is_c=is_carpenter(e)
            b_on=on_at(bronze_sp,t);c_on=on_at(carpenter_sp,t)
            both=(b_on and c_on) if bronze_status!='unavailable' and carpenter_status!='unavailable' else None
            half='attacking' if isinstance(x,(int,float)) and x>50 else ('defensive' if isinstance(x,(int,float)) and x<50 else 'halfway_or_missing')
            row={'date':date,'match':match,'minute':e.get('timeMin'),'second':e.get('timeSec'), 'player':player,'event_id':e.get('id'),
                 'x':x,'y':y,'half':half,'bronze_on':b_on if bronze_status!='unavailable' else '',
                 'carpenter_on':c_on if carpenter_status!='unavailable' else '',
                 'both_on':both if both is not None else '', 'lineup_status':f'{bronze_status}/{carpenter_status}'}
            throw_rows.append(row);match_throws.append(row)
            if both:count_both[player]+=1
            if is_b:maps['bronze'].append(row)
            if is_c:maps['carpenter'].append(row)
            if is_b or is_c:
                # First same-team pass/touch (type 1 or 61) after throw, within 12 seconds;
                # describes next involvement, NOT proof of actual throw-in recipient.
                followers=[a for a in ev[ix+1:] if 0 <= seconds(a)-t <=12 and a.get('contestantId')==chelsea_id and a.get('typeId') in (1,61) and a.get('id') != e.get('id')]
                first=followers[0] if followers else None
                next_two=followers[1] if len(followers)>1 else None
                next_three=followers[2] if len(followers)>2 else None
                next_rows.append({**row,'next_chelsea_player':name(first) if first else '',
                                  'next_event_type':first.get('typeId','') if first else '',
                                  'seconds_to_next':seconds(first)-t if first else '',
                                  'next_player_is_thrower':bool(first and first.get('playerId')==e.get('playerId')) if first else '',
                                  'next_x':first.get('x','') if first else '', 'next_y':first.get('y','') if first else '',
                                  'second_action_player':name(next_two) if next_two else '',
                                  'third_action_player':name(next_three) if next_three else '',
                                  'note':'First recorded Chelsea pass/touch ≤12 sec; NOT validated recipient'})
        for p, total in count_both.items():shared_rows.append({'date':date,'match':match,'player':p,'throws_both_on':total})
        for player_id, player_name, spans, status in [(bronze_id,'Lucy Bronze',bronze_sp,bronze_status),(carpenter_id,'Ellie Carpenter',carpenter_sp,carpenter_status)]:
            if not player_id:continue
            relevant=[e for e in ev if e.get('contestantId')==chelsea_id and e.get('playerId')==player_id and on_at(spans,seconds(e))]
            # Passes: exclude throw-ins and other dead ball qualifiers; open-play proxy only.
            passes=[e for e in relevant if e.get('typeId')==1 and not any(qual(e,q) is not None for q in (107,2,5,6,124))]
            open_rows.append({'date':date,'match':match,'player':player_name,'minutes_estimated':round(sum(b-a for a,b in spans)/60,1),
                              'lineup_status':status,'passes_excl_known_restarts':len(passes),
                              'passes_final_third':sum((e.get('x') or 0)>=66.67 for e in passes),
                              'passes_attacking_half':sum((e.get('x') or 0)>50 for e in passes),
                              'touches_final_third_proxy':sum(e.get('typeId') in (1,61,7,8,3,4,13) and (e.get('x') or 0)>=66.67 and qual(e,107) is None for e in relevant),
                              'note':'Pass/touch event proxies, not possession-adjusted; manual validation advised'})
        match_rows.append({'date':date,'match':match,'bronze_minutes_est':round(sum(b-a for a,b in bronze_sp)/60,1),
                           'carpenter_minutes_est':round(sum(b-a for a,b in carpenter_sp)/60,1),'shared_minutes_est':round(shared/60,1),
                           'bronze_status':bronze_status,'carpenter_status':carpenter_status,
                           'bronze_throws':sum(is_bronze(r) for r in match_throws),
                           'carpenter_throws':sum(is_carpenter(r) for r in match_throws),
                           'bronze_throws_both_on':sum(is_bronze(r) and r['both_on'] is True for r in match_throws),
                           'carpenter_throws_both_on':sum(is_carpenter(r) and r['both_on'] is True for r in match_throws),
                           'bronze_attacking':sum(is_bronze(r) and r['half']=='attacking' for r in match_throws),
                           'carpenter_attacking':sum(is_carpenter(r) and r['half']=='attacking' for r in match_throws)})
    write('chelsea_all_throwins.csv',throw_rows,['date','match','minute','second','player','event_id','x','y','half','bronze_on','carpenter_on','both_on','lineup_status'])
    write('bronze_carpenter_throw_followups.csv',next_rows,['date','match','minute','second','player','event_id','x','y','half','both_on','next_chelsea_player','next_event_type','seconds_to_next','next_player_is_thrower','next_x','next_y','second_action_player','third_action_player','note'])
    write('chelsea_shared_minutes_summary.csv',match_rows,['date','match','bronze_minutes_est','carpenter_minutes_est','shared_minutes_est','bronze_status','carpenter_status','bronze_throws','carpenter_throws','bronze_throws_both_on','carpenter_throws_both_on','bronze_attacking','carpenter_attacking'])
    write('bronze_carpenter_open_play_proxies.csv',open_rows,['date','match','player','minutes_estimated','lineup_status','passes_excl_known_restarts','passes_final_third','passes_attacking_half','touches_final_third_proxy','note'])
    pitch_svg(OUT/'bronze_throwin_map.svg',maps['bronze'],'Lucy Bronze: WSL throw-in locations')
    pitch_svg(OUT/'carpenter_throwin_map.svg',maps['carpenter'],'Ellie Carpenter: WSL throw-in locations')
    print('Unique matches:',processed,'unreadable:',invalid,'Chelsea matches:',len(match_rows))
    print('Chelsea throws:',len(throw_rows),'Bronze:',len(maps['bronze']),'Carpenter:',len(maps['carpenter']))
    for r in match_rows:print(r['date'],r['match'],'shared min',r['shared_minutes_est'],'throws while both on',r['bronze_throws_both_on'],r['carpenter_throws_both_on'])
    print('Reports and SVG maps in',OUT)

if __name__=='__main__':main()
