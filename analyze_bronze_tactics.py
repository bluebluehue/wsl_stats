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
 'Mapi Leon': ('London City','LCB','usual',['mapi leon','mapi león','mapí león','m. leon','m. león']),
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
MIN_APPEARANCES = 4  # editable: count games with recorded selected actions


# Standard Opta F24: 5=free kick, 6=corner, 107=throw, 124=goal kick,
# 279=kickoff. 2=CROSS and 4=THROUGH BALL: do not exclude these!
RESTART_Q = {5,6,107,124,279}
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



# Team-oriented Opta pitch units. Right side is assumed to be lower y;
# this convention MUST be checked against broadcast footage before asserting
# which sideline corresponds to a player's actual right.
THIRDS = ('Defensive', 'Middle', 'Final')
LANES = ('Right wide', 'Right inside', 'Centre', 'Left inside', 'Left wide')

def pitch_cell(x, y):
    if x is None or y is None or not (0 <= x <= 100 and 0 <= y <= 100):
        return None
    ix = min(2, int(x * 3 / 100))
    iy = min(4, int(y / 20))
    return ix, iy


def zonal_pitch_svg(path, player, counts, total, title):
    import html
    # Vertical pitch in team-oriented Opta coordinates: attack from left to right;
    # top horizontal band is y=0..20 (presumed right flank).
    max_c = max(counts.values(), default=1) or 1
    shapes = []
    ox, oy, w, h = 60, 90, 900, 440
    for ix in range(3):
        for iy in range(5):
            n = counts.get((ix, iy), 0)
            x0, y0 = ox + ix*w/3, oy+iy*h/5
            alpha = .12 + .80*(n/max_c) if n else .06
            shapes.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w/3:.1f}" height="{h/5:.1f}" fill="#f6b958" fill-opacity="{alpha:.3f}" stroke="white" stroke-opacity=".3"/>')
            shapes.append(f'<text x="{x0+w/6:.1f}" y="{y0+h/10+5:.1f}" font-family="Arial" text-anchor="middle" font-weight="bold" fill="white" font-size="19">{n} ({(100*n/total if total else 0):.1f}%)</text>')
    for ix, label in enumerate(THIRDS):
        shapes.append(f'<text x="{ox+(ix+.5)*w/3:.1f}" y="{oy-16}" font-family="Arial" font-size="18" text-anchor="middle" fill="white">{label} third</text>')
    for iy, label in enumerate(LANES):
        shapes.append(f'<text x="{ox-10}" y="{oy+(iy+.5)*h/5+4:.1f}" font-family="Arial" font-size="13" text-anchor="end" fill="white">{label}</text>')
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="620" viewBox="0 0 1080 620">'
      '<rect width="100%" height="100%" fill="#18372e"/>'
      f'<text x="45" y="35" font-family="Arial" font-size="24" fill="white">{html.escape(title)}</text>'
      f'<text x="45" y="61" font-family="Arial" font-size="15" fill="white">{total} event points | percentage of that player\'s selected events | attacking →</text>'
      + ''.join(shapes) + '</svg>', encoding='utf-8')

def main():
    if not SOURCE.is_dir():
        raise SystemExit('Missing raw_opta_events/; run from repository root')
    OUT.mkdir(exist_ok=True)
    records, summaries, contexts = [], [], []
    all_points = defaultdict(list)
    zone_event_rows = []
    zone_by_match_rows = []
    player_zone_counts = defaultdict(Counter)
    player_pass_zone_counts = defaultdict(Counter)
    player_other_zone_counts = defaultdict(Counter)
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
            match_zone_counts = Counter()
            for e in action_events:
                x0, y0 = num(e.get('x')), num(e.get('y'))
                cell = pitch_cell(x0, y0)
                if cell is None:
                    continue
                family = 'pass' if e.get('typeId') == PASS else 'other_action'
                ix, iy = cell
                match_zone_counts[(ix,iy,family)] += 1
                player_zone_counts[name][(ix,iy)] += 1
                (player_pass_zone_counts if family == 'pass' else player_other_zone_counts)[name][(ix,iy)] += 1
                zone_event_rows.append({'date':day, 'match':fixture,'player':name,
                    'team':ROSTER[name][0], 'usual_role':ROSTER[name][1],
                    'event_id':e.get('id',''),'type_id':e.get('typeId',''),
                    'event_family':family,'x':x0,'y':y0,
                    'third':THIRDS[ix], 'lane':LANES[iy],
                    'zone':THIRDS[ix]+' / '+LANES[iy]})
            for ix in range(3):
                for iy in range(5):
                    for family in ('pass','other_action'):
                        zone_by_match_rows.append({'date':day,'match':fixture,'player':name,
                            'team':ROSTER[name][0], 'usual_role':ROSTER[name][1],
                            'third':THIRDS[ix], 'lane':LANES[iy], 'event_family':family,
                            'actions':match_zone_counts[(ix,iy,family)]})
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
    # A 4-match eligibility threshold is a pragmatic sample-size filter, not
    # proof of starts, centre-back minutes, nor verified positional deployment.
    eligible = {name for name, count in appearances.items() if count >= MIN_APPEARANCES}
    qualified = []
    for r in totals:
        enriched = dict(r)
        enriched['qualified_4plus'] = 'yes' if r['player'] in eligible else 'no'
        enriched['reason'] = ('>=4 matches with selected events' if r['player'] in eligible
                              else '<4 matches with selected events; retained for transparency')
        qualified.append(enriched)
    write('comparison_full_with_eligibility.csv', qualified, list(qualified[0].keys()))
    write('comparison_4plus.csv', [r for r in qualified if r['qualified_4plus']=='yes'],
          list(qualified[0].keys()))
    zone_fields=['date','match','player','team','usual_role','event_id','type_id','event_family','x','y','third','lane','zone']
    write('zonal_events.csv', zone_event_rows,zone_fields)
    write('zonal_by_match.csv',zone_by_match_rows,
          ['date','match','player','team','usual_role','third','lane','event_family','actions'])
    zone_totals=[]
    for r in totals:
        name=r['player']
        player_total = sum(player_zone_counts[name].values())
        pass_total = sum(player_pass_zone_counts[name].values())
        nonpass_total = sum(player_other_zone_counts[name].values())
        for ix in range(3):
            for iy in range(5):
                n=player_zone_counts[name][(ix,iy)]
                pn=player_pass_zone_counts[name][(ix,iy)]
                on=player_other_zone_counts[name][(ix,iy)]
                zone_totals.append({'player':name,'team':r['team'],'usual_role':r['usual_role'],
                   'matches_with_events':r['matches_with_events'],
                   'qualified_4plus':'yes' if name in eligible else 'no',
                   'position_verified':'no', 'third':THIRDS[ix], 'lane':LANES[iy],
                   'action_count':n,'pct_player_actions':round(n/player_total*100,2) if player_total else '',
                   'pass_count':pn,'pct_player_passes':round(pn/pass_total*100,2) if pass_total else '',
                   'other_action_count':on,
                   'pct_player_other_actions':round(on/nonpass_total*100,2) if nonpass_total else ''})
        if name in eligible and player_total:
            zonal_pitch_svg(OUT/('zones_'+name.lower().replace(' ','_').replace("'",'')+'.svg'),
                            name,player_zone_counts[name],player_total,name+' — open-play zones')
    zone_total_columns=['player','team','usual_role','matches_with_events','qualified_4plus',
              'position_verified','third','lane','action_count','pct_player_actions',
              'pass_count','pct_player_passes','other_action_count','pct_player_other_actions']
    write('zonal_player_totals.csv',zone_totals,zone_total_columns)
    write('zonal_4plus.csv',[r for r in zone_totals if r['qualified_4plus']=='yes'],zone_total_columns)
    fingerprint=[]
    for r in totals:
        name=r['player']
        c=player_zone_counts[name]
        n=sum(c.values())
        pn=sum(player_pass_zone_counts[name].values())
        fingerprint.append({'player':name,'team':r['team'],'usual_role':r['usual_role'],
          'matches_with_events':r['matches_with_events'],
          'qualified_4plus':'yes' if name in eligible else 'no',
          'position_verified':'no', 'action_count':n,'pass_count':pn,
          'right_wide_final_count':c[(2,0)],
          'right_wide_final_pct':round(100*c[(2,0)]/n,2) if n else '',
          'right_side_attacking_half_count':sum(c[(ix,iy)] for ix in (1,2) for iy in (0,1)),
          'right_side_attacking_half_pct':round(100*sum(c[(ix,iy)] for ix in (1,2) for iy in (0,1))/n,2) if n else '',
          'central_attacking_half_pct':round(100*sum(c[(ix,2)] for ix in (1,2))/n,2) if n else '',
          'final_third_count':sum(c[(2,iy)] for iy in range(5)),
          'final_third_pct':round(100*sum(c[(2,iy)] for iy in range(5))/n,2) if n else ''})
    write('zonal_fingerprint_all.csv',fingerprint,list(fingerprint[0].keys()))
    write('zonal_fingerprint_4plus.csv',[r for r in fingerprint if r['qualified_4plus']=='yes'],
         list(fingerprint[0].keys()))
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
ZONAL UPDATE: pitch divided into 3 thirds x 5 lanes. Separate pass and
non-pass selected action counts; the latter are NOT synonymous with defensive
interventions. 'Right' and 'left' assume y=0 right in Opta player-team frame;
VERIFY this convention against footage. Qualified >=4 means >=4 matches with
recorded selected events, NOT >=4 starts or >=4 games as CB. Injury absence
is not penalized beyond the sample threshold. Files labeled *_4plus are the
suggested comparison, while *_all preserves transparency. Match-by-match
positional verification remains required before making strict CB-only claims.
''',encoding='utf-8')
    print('Unique WSL matches:',matches_seen,'Unreadable/invalid:',rejected,'Recorded open-play passes:',accepted)
    for r in totals:
        print(r['player'],'matched matches:',r['matches_with_events'],'passes:',r['open_pass_attempts'],'actions:',r['open_action_points'])
    print('Eligible >=4 appearances:',len(eligible),'of',len(ROSTER))
    print('Results:',OUT)
    phase_analysis()

FOLLOWUP_SECONDS = 12
RESTART_NAMES = {5:'free_kick',6:'corner',107:'throw_in',124:'goal_kick',279:'kick_off'}
KNOWN_NON_CB = {('Alex Greenwood','2026-09-06'), ('Alex Greenwood','2026-09-26'), ('Veerle Buurman','2026-09-13')}

def match_seconds(e):
    try: return int(e.get('timeMin',0))*60+int(e.get('timeSec',0))
    except (TypeError,ValueError): return None

def direct_restart(e):
    if e.get('typeId') != PASS: return None
    for key in (6,5,107,124,279):
        if key in qids(e): return RESTART_NAMES[key]
    return None

def phase_analysis():
    audits, per_match = [], []
    files_seen=set()
    keys=('all_action','all_wide','direct_restart','direct_restart_wide','restart_followup_12s','restart_followup_12s_wide','corner_situation_qualifier','corner_situation_qualifier_wide','established_open_proxy','established_open_proxy_wide','crosses')
    for file in sorted(SOURCE.glob('*.json')):
        try:
            d=json.loads(file.read_text(encoding='utf-8-sig'))
            info=d['matchInfo']; evs=d['liveData']['event']
            if str((info.get('competition') or {}).get('competitionCode','')).upper()!='WSL': continue
            mid=str(info.get('id') or file.stem)
            if mid in files_seen: continue
            files_seen.add(mid)
            teams={str(x['id']):x.get('name','') for x in info.get('contestant',[])}
            day=str(info.get('localDate') or info.get('date') or '')[:10]
            fixture=info.get('description','')
        except (ValueError,KeyError,TypeError,OSError): continue
        events=sorted(enumerate(evs),key=lambda ie:(match_seconds(ie[1]) if match_seconds(ie[1]) is not None else 999999,ie[0]))
        active={}; counts=defaultdict(Counter); names_in_match=set()
        for _,e in events:
            t=match_seconds(e); cid=str(e.get('contestantId',''))
            restart=direct_restart(e)
            if restart is not None:
                active.clear(); active[cid]=(t,restart,e.get('id',''))
            elif t is not None:
                for other,(t0,kind,eventid) in list(active.items()):
                    if t0 is None or t-t0 > FOLLOWUP_SECONDS or t<t0: active.pop(other,None)
                if e.get('typeId') == PASS and e.get('outcome')==1:
                    for other in list(active):
                        if other!=cid:active.pop(other,None)
            name=player_label(e.get('playerName'))
            if name not in ROSTER or ROSTER[name][0].casefold() not in teams.get(cid,'').casefold(): continue
            names_in_match.add(name)
            if e.get('typeId') not in ACTION_TYPES: continue
            x,y=num(e.get('x')),num(e.get('y'))
            cell=pitch_cell(x,y)
            if cell is None: continue
            if restart is not None: phase='direct_restart'
            elif cid in active and active[cid][0] is not None and t is not None and t>active[cid][0] and t-active[cid][0]<=FOLLOWUP_SECONDS:
                phase='restart_followup_12s'
            elif 96 in qids(e): phase='corner_situation_qualifier'
            else: phase='established_open_proxy'
            flank_cell = (2,0) if ROSTER[name][1] in ('RCB','RB') else (2,4)
            wide=int(cell==flank_cell)
            audits.append({'date':day,'match':fixture,'player':name,'usual_role':ROSTER[name][1],
                'known_non_cb':'yes' if (name,day) in KNOWN_NON_CB else 'no',
                'event_id':e.get('id',''),'minute':e.get('timeMin',''),'second':e.get('timeSec',''),
                'type_id':e.get('typeId'),'qualifier_2_cross':int(2 in qids(e)),
                'qualifier_4_through_ball':int(4 in qids(e)),
                'phase':phase,'restart_kind':restart or (active[cid][1] if cid in active else ''),
                'x':x,'y':y,'third':THIRDS[cell[0]],'lane':LANES[cell[1]],
                'own_flank_final_wide':wide,'is_pass':int(e.get('typeId')==PASS)})
            counts[name]['all_action']+=1; counts[name]['all_wide']+=wide
            counts[name][phase]+=1
            if wide:counts[name][phase+'_wide']+=1
            if e.get('typeId')==PASS: counts[name]['crosses']+=int(2 in qids(e))
        for name in names_in_match:
            c=counts[name]
            per_match.append({'date':day,'match':fixture,'player':name,'team':ROSTER[name][0],
                'usual_role':ROSTER[name][1], 'known_non_cb':'yes' if (name,day) in KNOWN_NON_CB else 'no',
                **{k:c[k] for k in keys}})
    audit_cols=['date','match','player','usual_role','known_non_cb','event_id','minute','second','type_id','qualifier_2_cross','qualifier_4_through_ball','phase','restart_kind','x','y','third','lane','own_flank_final_wide','is_pass']
    write('setpiece_event_audit.csv',audits,audit_cols)
    write('setpiece_by_match.csv',per_match,['date','match','player','team','usual_role','known_non_cb',*keys])
    combined=[]
    for name in ROSTER:
        rows=[r for r in per_match if r['player']==name]
        cb_rows=[r for r in rows if r['known_non_cb']!='yes']
        result={'player':name,'team':ROSTER[name][0],'usual_role':ROSTER[name][1],
            'matched_appearances':len(rows),'qualified_4plus':'yes' if len(rows)>=MIN_APPEARANCES else 'no',
            'known_non_cb_matches_excluded':len(rows)-len(cb_rows),
            'remaining_appearances':len(cb_rows),
            'remaining_4plus':'yes' if len(cb_rows)>=MIN_APPEARANCES else 'no',
            'position_verified_all_matches':'no'}
        for group,subset in [('all',rows),('after_known_lb_exclusions',cb_rows)]:
            for key in keys:result[group+'_'+key]=sum(r[key] for r in subset)
            den=result[group+'_established_open_proxy']; n=result[group+'_established_open_proxy_wide']
            result[group+'_strict_wide_pct']=round(100*n/den,2) if den else ''
            den2=result[group+'_all_action']-result[group+'_direct_restart']
            n2=result[group+'_all_wide']-result[group+'_direct_restart_wide']
            result[group+'_direct_only_wide_pct']=round(100*n2/den2,2) if den2 else ''
        combined.append(result)
    cols=list(combined[0]); write('setpiece_comparison_all.csv',combined,cols)
    eligible=[r for r in combined if r['usual_role'] in ('RCB','LCB') and r['remaining_4plus']=='yes']
    eligible.sort(key=lambda r:-(r['after_known_lb_exclusions_strict_wide_pct'] or 0))
    write('setpiece_cb_ranking_4plus.csv',eligible,cols)
    (OUT/'SETPIECE_AUDIT_README.txt').write_text('''RESTART-CONTAMINATION SENSITIVITY ANALYSIS

Opta: 2=CROSS, 4=THROUGH BALL (both retained); 5=free kick, 6=corner,
107=throw-in, 124=goal kick, 279=kickoff (direct restart excluded).
Direct-only excludes directly tagged restart passes. Strict proxy also
excludes selected actions of the same team in 12 seconds after a restart.
A successful opposing pass ends the window. Corner-situation qualifier 96
is excluded separately. The 12-second window is a SENSITIVITY HEURISTIC,
not Opta's definition of open play. It can exclude genuine live play or
retain later set-piece phases; defensive actions by the restarting team
may also be within the exclusion window.

IMPORTANT: Compared to older runs, the pass mapping fix changes BOTH the
numerator and denominator. Compare direct_only vs strict from THIS run to
isolate the effect of the follow-up-window exclusion.

Known LB appearances excluded only in 'after_known_lb_exclusions':
Greenwood Sep 6/Sep 26, Buurman Sep 13. Other appearances remain position
UNVERIFIED. Minimum 4 is appearances with events after those exclusions,
NOT four confirmed CB starts. 'Own flank' assumes team-oriented y=0 right.
No tracking, minutes/possession adjustments, or verified carries.
''',encoding='utf-8')
    print('Set-piece audit:',len(audits),'action events; eligible CB:',len(eligible))

if __name__ == '__main__':
    main()
