#!/usr/bin/env python3
"""Read-only WSL Opta F24 open-play event analysis focused on Lucy Bronze.

Runs from repository root; reads raw_opta_events/*.json and writes ONLY to
bronze_tactical_analysis/. No external packages. Event locations ≠ player tracking;
pass progression ≠ progressive carry. No inference about off-ball positioning.
"""
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

SOURCE = Path('raw_opta_events')
OUT = Path('bronze_tactical_analysis')
# Player names checked against case-insensitive substrings. Includes a Chelsea teammate
# and the five RCBs from the original heatmap comparison.
# User-provided usual positions. "review" entries may play another position;
# preserve them as candidates only, not verified CB match appearances.
# No Birmingham/Everton RCB imputation.
ROSTER = {
 'Lucy Bronze': ('Chelsea','RCB','usual',['bronze']),
 'Kadeisha Buchanan': ('Chelsea','LCB','review',['buchanan']),
 'Veerle Buurman': ('Chelsea','LCB','review',['buurman']),
 'Ellie Carpenter': ('Chelsea','RB','usual',['carpenter']),
 'Jade Rose': ('Manchester City','RCB','usual',['jade rose','j. rose']),
 'Alex Greenwood': ('Manchester City','LCB','review',['greenwood']),
 'Leah Williamson': ('Arsenal','RCB','usual',['williamson']),
 'Steph Catley': ('Arsenal','LCB','review',['catley']),
 'Lotte Wubben-Moy': ('Arsenal','LCB','review',['wubben-moy','wubben moy']),
 'Toko Koga': ('Tottenham','RCB','usual',['koga']),
 'Amanda Nilden': ('Tottenham','LCB','usual',['nilden','nildén']),
 'Isa Kardinaal': ('London City','RCB','usual',['kardinaal']),
 'Mapi Leon': ('London City','LCB','usual',['mapi leon','mapí león','m. leon','m. león','maría pilar león','maria pilar leon','maría león','maria leon','m. p. leon','m. p. león','pilar león','pilar leon']),
 'Jenna Clark': ('Liverpool','RCB','usual',['jenna clark','j. clark']),
 'Grace Fisk': ('Liverpool','LCB','usual',['grace fisk','g. fisk']),
 'Maya Le Tissier': ('Manchester United','RCB','usual',['le tissier','letissier']),
 'Dominique Janssen': ('Manchester United','LCB','usual',['janssen']),
 'Caitlin Hayes': ('Brighton','RCB','usual',['hayes']),
 'Gabby George': ('Brighton','LCB','usual',['gabby george','g. george']),
 'Laia Codina': ('West Ham','RCB','usual',['codina']),
 'Estelle Cascarino': ('West Ham','LCB','usual',['cascarino']),
 'Molly Bartrip': ('Crystal Palace','RCB','usual',['bartrip']),
 'Aimee Everett': ('Crystal Palace','LCB','usual',['everett']),
 'Millie Turner': ('Birmingham','LCB','usual',['millie turner','m. turner']),
 'Anna Patten': ('Aston Villa','RCB','usual',['anna patten','a. patten']),
 'Mathilde Harviken': ('Aston Villa','LCB','usual',['harviken']),
 "Elisha N'Dow": ('Charlton','RCB','usual',["n'dow",'ndow','n’dow']),
}
TARGETS = {n: v[3] for n,v in ROSTER.items()}

# Opta pass qualifiers for restarts. 107=throw-in; 2=corner; 5=free kick;
# 6=goal kick; 124=goalkeeper throw (exclude conservatively).
# 4=indirect FK in some Opta dictionaries; 24=free kick (variant);
# 26=corner taken (variant). Treat as possible restarts for transparent filtering.
RESTART_Q = {2, 4, 5, 6, 24, 26, 107, 124}
PASS = 1
# Other events with reliably attributable ball action locations; these are
# ACTION points, NOT an exhaustive list of touches or actual player positions.
ACTION_TYPES = {1, 3, 4, 7, 8, 12, 13, 15, 49, 61, 74}


def qids(e):
    return {q.get('qualifierId') for q in (e.get('qualifier') or [])}


def qvalue(e, qual_id):
    return next((q.get('value') for q in (e.get('qualifier') or []) if q.get('qualifierId') == qual_id), None)


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def player_label(name):
    name = str(name or '').casefold()
    for label, pieces in TARGETS.items():
        if any(piece in name for piece in pieces):
            return label
    return None


def write(name, rows, columns):
    with (OUT / name).open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def is_open_pass(e):
    return e.get('typeId') == PASS and not qids(e).intersection(RESTART_Q)


def pass_end(e):
    # Opta qualifiers 140 and 141 are pass end x/y (verify by sampling).
    return num(qvalue(e, 140)), num(qvalue(e, 141))


def zone(x, y):
    if x is None or y is None:
        return 'unknown'
    third = 'defensive' if x < (100 / 3) else ('middle' if x < (200 / 3) else 'final')
    lane = 'lower_touchline' if y <= 15 else ('upper_touchline' if y >= 85 else ('central' if 33 <= y <= 67 else 'half_space'))
    return third + '_' + lane


def pitch_svg(path, points, title):
    # Pitch axes: attacking direction to the right; Opta 0..100 per team.
    import html
    width, height, x0, y0, pw, ph = 940, 560, 50, 78, 840, 415
    shapes = [f'<rect x="{x0}" y="{y0}" width="{pw}" height="{ph}" fill="#225b49" stroke="white" stroke-width="2"/>',
              f'<path d="M{x0+pw/2} {y0}V{y0+ph}" stroke="white" stroke-width="2"/>',
              f'<circle cx="{x0+pw/2}" cy="{y0+ph/2}" r="62" stroke="white" stroke-width="2" fill="none"/>',
              f'<rect x="{x0}" y="{y0+105}" width="125" height="205" stroke="white" stroke-width="2" fill="none"/>',
              f'<rect x="{x0+pw-125}" y="{y0+105}" width="125" height="205" stroke="white" stroke-width="2" fill="none"/>']
    for x, y in points:
        xx = x0 + x * pw / 100
        yy = y0 + (100-y) * ph / 100
        shapes.append(f'<circle cx="{xx:.2f}" cy="{yy:.2f}" r="4" fill="#fed06b" fill-opacity=".26"/>')
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
                    '<rect width="100%" height="100%" fill="#172f28"/>'
                    f'<text x="50" y="37" fill="white" font-family="Arial" font-size="22">{html.escape(title)}</text>'
                    f'<text x="50" y="523" fill="white" font-family="Arial" font-size="14">Attacking direction → | {len(points)} event locations | Not an off-ball position map</text>'
                    + ''.join(shapes) + '</svg>', encoding='utf-8')


def main():
    if not SOURCE.is_dir():
        raise SystemExit('Missing raw_opta_events/; run from repository root')
    OUT.mkdir(exist_ok=True)
    records, summaries, contexts = [], [], []
    all_points = defaultdict(list)
    files_seen, matches_seen, rejected, accepted = set(), 0, 0, 0
    appearances = Counter()

    for file in sorted(SOURCE.glob('*.json')):
        try:
            d = json.loads(file.read_text(encoding='utf-8-sig'))
            info = d['matchInfo']
            events = d['liveData']['event']
            mid = str(info.get('id') or file.stem)
            if mid in files_seen:
                continue
            files_seen.add(mid)
            competition = str((info.get('competition') or {}).get('competitionCode') or '')
            if competition.upper() != 'WSL':
                continue
            teams = {str(c['id']): c.get('name', '') for c in info.get('contestant', [])}
            if len(teams) != 2 or not isinstance(events, list):
                rejected += 1
                continue
        except (KeyError, TypeError, ValueError, OSError):
            rejected += 1
            continue
        matches_seen += 1
        day = info.get('localDate') or info.get('date') or ''
        fixture = info.get('description') or ''
        by_player = defaultdict(list)
        for e in events:
            name = player_label(e.get('playerName'))
            team = teams.get(str(e.get('contestantId')), '')
            if name is None or not team:
                continue
            # Require club match as well as player alias; avoids 'J. Clark' matches
            # for similarly named players on other clubs.
            expected = ROSTER[name][0]
            if expected.casefold() not in team.casefold():
                continue
            by_player[name].append(e)

        for name, evs in by_player.items():
            appearances[name] += 1
            passes = [e for e in evs if is_open_pass(e)]
            action_events = [e for e in evs if e.get('typeId') in ACTION_TYPES
                             and not (e.get('typeId') == PASS and not is_open_pass(e))]
            valid_p = [(e, num(e.get('x')), num(e.get('y')), *pass_end(e)) for e in passes]
            valid_p = [(e, x, y, ex, ey) for e,x,y,ex,ey in valid_p if x is not None and y is not None]
            points = [(num(e.get('x')),num(e.get('y'))) for e in action_events]
            points = [(x,y) for x,y in points if x is not None and y is not None and 0<=x<=100 and 0<=y<=100]
            all_points[name].extend(points)
            # Passing percentages are per pass attempt, not per minute nor adjusted for possessions.
            progressive = 0
            into_final = 0
            forward_10 = 0
            completed = 0
            with_endpoint = 0
            for e,x,y,ex,ey in valid_p:
                if ex is not None and ey is not None:
                    with_endpoint += 1
                    delta = ex - x
                    if delta >= 10:
                        forward_10 += 1
                    if x < (200/3) <= ex:
                        into_final += 1
                    # Approx progressive-pass definition: advances ball >=25% of
                    # remaining distance to goal, with >=10 units of x progress.
                    if delta >= 10 and delta >= 0.25 * (100-x):
                        progressive += 1
                completed += e.get('outcome') == 1
                records.append({'date':day,'match':fixture,'team':next((teams[str(e['contestantId'])] for _ in [0]),''),
                                'player':name,'minute':e.get('timeMin',''),'second':e.get('timeSec',''),
                                'event_id':e.get('id',''),'x':x,'y':y,'end_x':ex if ex is not None else '',
                                'end_y':ey if ey is not None else '', 'origin_zone':zone(x,y),
                                'completed':int(e.get('outcome')==1),
                                'forward_10_units':int(ex is not None and ex-x>=10),
                                'into_final_third':int(ex is not None and x<200/3<=ex),
                                'progressive_pass_proxy':int(ex is not None and (ex-x)>=10 and (ex-x)>=.25*(100-x))})
            n = len(points)
            summaries.append({'date':day,'match':fixture,'player':name, 'team':ROSTER[name][0], 'usual_role':ROSTER[name][1], 'position_review':('needs_match_verification' if ROSTER[name][2]=='review' else 'usual_role_not_lineup_verified'),
                'open_pass_attempts':len(passes),'passes_with_start_coords':len(valid_p),
                'passes_with_end_coords':with_endpoint,'completed_open_passes':completed,
                'pass_origin_attacking_half':sum(x>50 for _,x,_,_,_ in valid_p),
                'pass_origin_final_third':sum(x>=200/3 for _,x,_,_,_ in valid_p),
                'pass_origin_wide_right_proxy':sum(y<=20 for _,_,y,_,_ in valid_p),
                'forward_pass_10plus':forward_10,'passes_into_final_third':into_final,
                'progressive_pass_proxy':progressive,
                'open_action_points':n,
                'open_actions_attacking_half':sum(x>50 for x,y in points),
                'open_actions_final_third':sum(x>=200/3 for x,y in points),
                'open_actions_touchline_band':sum(y<=15 or y>=85 for x,y in points),
                'open_actions_wide_right_proxy':sum(y<=20 for x,y in points),
                'avg_action_x':round(sum(x for x,y in points)/n,2) if n else '',
                'avg_action_y':round(sum(y for x,y in points)/n,2) if n else ''})
            accepted += len(valid_p)

    write('open_play_passes.csv', records, ['date','match','team','player','minute','second','event_id','x','y','end_x','end_y','origin_zone','completed','forward_10_units','into_final_third','progressive_pass_proxy'])
    fields = ['date','match','player','team','usual_role','position_review','open_pass_attempts','passes_with_start_coords','passes_with_end_coords','completed_open_passes','pass_origin_attacking_half','pass_origin_final_third','pass_origin_wide_right_proxy','forward_pass_10plus','passes_into_final_third','progressive_pass_proxy','open_action_points','open_actions_attacking_half','open_actions_final_third','open_actions_touchline_band','open_actions_wide_right_proxy','avg_action_x','avg_action_y']
    write('open_play_by_match.csv', summaries, fields)
    totals = []
    for name in TARGETS:
        rows = [r for r in summaries if r['player']==name]
        nums = {f:sum(r[f] for r in rows) for f in fields[6:-2]}
        actions = nums.get('open_action_points',0)
        p = nums.get('open_pass_attempts',0)
        totals.append({'player':name,'team':ROSTER[name][0],'usual_role':ROSTER[name][1],'position_review':('needs_match_verification' if ROSTER[name][2]=='review' else 'usual_role_not_lineup_verified'),'matches_with_events':len(rows),**nums,
                       'pct_actions_attacking_half':round(100*nums['open_actions_attacking_half']/actions,1) if actions else '',
                       'pct_actions_final_third':round(100*nums['open_actions_final_third']/actions,1) if actions else '',
                       'pct_action_touchline_band':round(100*nums['open_actions_touchline_band']/actions,1) if actions else '',
                       'pct_pass_origins_attacking_half':round(100*nums['pass_origin_attacking_half']/p,1) if p else '',
                       'pct_pass_origins_final_third':round(100*nums['pass_origin_final_third']/p,1) if p else ''})
    write('open_play_player_comparison.csv',totals,list(totals[0].keys()))
    for name, points in all_points.items():
        pitch_svg(OUT/('open_actions_'+name.lower().replace(' ','_')+'.svg'),points,name+' — open-play event locations')
    write('position_review.csv', [dict(date=r['date'],match=r['match'],team=r['team'],player=r['player'],usual_role=r['usual_role'],position_review=r['position_review'],actual_match_position='',verified_by='') for r in summaries], ['date','match','team','player','usual_role','position_review','actual_match_position','verified_by'])
    (OUT/'README_METHODS.txt').write_text('''WSL OPEN-PLAY EVENT ANALYSIS: INTERPRETATION NOTES

Sources: cached Opta event data; deduplicated by match ID; WSL competition only.
Throw-in passes (qualifier 107) and common other pass-restart qualifiers are excluded.
Actions shown in maps are selected event-location types, NOT tracking heatmaps,
not all true touches, and not players\' positions between recorded events.
Pass end coordinates come from qualifier 140/141; x is attacking direction 0-100.
Wide-right proxy y<=20 assumes Opta team-oriented y increases from its right
touchline towards its left. Verify this orientation for a known filmed action.
'Progressive pass proxy' requires >=10 x-unit advance AND >=25% of the remaining
x-distance to the opponent goal; NOT a standard Opta progressive-pass metric.
These are raw event counts/percentages, NOT per-90 or possession adjusted.
Matches missing from raw_opta_events are not included. Empty target player rows
mean player label was not matched or the player's club's feed was not available.
USUAL ROLE comes from the user's roster, not match lineup verification.
Players who sometimes start elsewhere are explicitly marked NEEDS_MATCH_VERIFICATION.
Even 'usual' positions are NOT confirmed on every date. Do not interpret
these aggregates as CB-only appearances until position_review.csv is checked.
Names are matched by aliases PLUS club; please inspect unmatched or ambiguous names.
Event coverage does not establish minutes played or centre-back position.
No carry distances, off-ball runs, or positional rotations are claimed.
''',encoding='utf-8')
    print('Unique WSL matches:',matches_seen,'Unreadable/invalid:',rejected,'Recorded open-play passes:',accepted)
    for r in totals:
        print(r['player'],'matched matches:',r['matches_with_events'],'passes:',r['open_pass_attempts'],'actions:',r['open_action_points'])
    print('Results:',OUT)

if __name__ == '__main__':
    main()
