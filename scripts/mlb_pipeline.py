#!/usr/bin/env python3
"""
MLB Auto-Pipeline — runs every 20 min via GitHub Actions
Full port of the JS model: fetches live Steamer projections from FanGraphs,
applies confirmed lineups, computes win probabilities, sends Telegram alerts.
"""

import os, json, math, time, requests, datetime, unicodedata, re
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore

# ── Firebase ─────────────────────────────────────────────────────────────
SA_JSON = os.environ.get('FIREBASE_SA_JSON', '')
cred = credentials.Certificate(json.loads(SA_JSON))
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Config ────────────────────────────────────────────────────────────────
RENDER_URL     = 'https://mlb-model-xwti.onrender.com'
ODDS_API_KEY   = '16381a969b24dad9592530007f8b51f9'
TG_TOKEN       = '8699040384:AAHGUYll31NgDPKFthDMB0_YTiKOVSOdF1A'
TG_CHAT        = '-5110059891'
ET             = ZoneInfo('America/New_York')
EDGE_THRESHOLD = 0.03
KELLY_FRACTION = 0.5
KELLY_MAX_PCT  = 3.0

TEAMS_30 = ['ARI','ATH','ATL','BAL','BOS','CHC','CHW','CIN','CLE','COL',
            'DET','HOU','KCR','LAA','LAD','MIA','MIL','MIN','NYM','NYY',
            'PHI','PIT','SDP','SEA','SFG','STL','TBR','TEX','TOR','WSN']

FG_TEAM_MAP = {'SF':'SFG','SD':'SDP','KC':'KCR','TB':'TBR','WSH':'WSN',
               'OAK':'ATH','CWS':'CHW','LAA':'LAA','LAD':'LAD'}

MLB_ID_TO_ABR = {
    133:'ATH',137:'SFG',138:'STL',139:'TBR',140:'TEX',141:'TOR',142:'MIN',
    143:'PHI',144:'ATL',145:'CHW',146:'MIA',147:'NYY',158:'MIL',
    108:'LAA',109:'ARI',110:'BAL',111:'BOS',112:'CHC',113:'CIN',114:'CLE',
    115:'COL',116:'DET',117:'HOU',118:'KCR',119:'LAD',120:'WSN',121:'NYM',
    134:'PIT',135:'SDP',136:'SEA',
}

NAME_TO_ABR = {
    'Arizona Diamondbacks':'ARI','Athletics':'ATH','Atlanta Braves':'ATL',
    'Baltimore Orioles':'BAL','Boston Red Sox':'BOS','Chicago Cubs':'CHC',
    'Chicago White Sox':'CHW','Cincinnati Reds':'CIN','Cleveland Guardians':'CLE',
    'Colorado Rockies':'COL','Detroit Tigers':'DET','Houston Astros':'HOU',
    'Kansas City Royals':'KCR','Los Angeles Angels':'LAA','Los Angeles Dodgers':'LAD',
    'Miami Marlins':'MIA','Milwaukee Brewers':'MIL','Minnesota Twins':'MIN',
    'New York Mets':'NYM','New York Yankees':'NYY','Philadelphia Phillies':'PHI',
    'Pittsburgh Pirates':'PIT','San Diego Padres':'SDP','Seattle Mariners':'SEA',
    'San Francisco Giants':'SFG','St. Louis Cardinals':'STL','Tampa Bay Rays':'TBR',
    'Texas Rangers':'TEX','Toronto Blue Jays':'TOR','Washington Nationals':'WSN',
}

# ── Math helpers (exact port of JS) ──────────────────────────────────────
def impl(odds):
    odds = float(odds)
    return 100/(odds+100) if odds >= 0 else abs(odds)/(abs(odds)+100)

def p_to_american(p):
    if p <= 0 or p >= 1: return 0
    if p >= 0.5: return -round((p/(1-p))*100)
    return round(((1-p)/p)*100)

def calc_kelly(odds, edge_pct):
    if edge_pct <= 0: return 0
    dec = (odds/100+1) if odds >= 0 else (100/abs(odds)+1)
    b = dec - 1
    if b <= 0: return 0
    p = impl(odds) + edge_pct/100
    q = 1 - p
    kelly = (b*p - q) / b
    return max(0, kelly) * 100

def norm_name(name):
    """Normalize player name for matching — port of JS normName()"""
    if not name: return ''
    s = unicodedata.normalize('NFD', name)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]', '', s.lower())

def fg_team(t):
    return FG_TEAM_MAP.get(t, t)

def get_et_date(utc_str):
    d = datetime.datetime.fromisoformat(utc_str.replace('Z','+00:00'))
    return d.astimezone(ET).strftime('%Y-%m-%d')

def fmt_time_et(utc_str):
    try:
        d = datetime.datetime.fromisoformat(utc_str.replace('Z','+00:00'))
        return d.astimezone(ET).strftime('%-I:%M %p ET')
    except: return ''

def today_et():
    return datetime.datetime.now(ET).strftime('%Y-%m-%d')

def fmt_odds(od):
    return f'+{od}' if od >= 0 else str(od)

# ── Model functions (exact port of JS) ────────────────────────────────────

def get_ip_start(p):
    """Port of JS getIpStart() — no IP overrides in pipeline"""
    gs = p.get('gs', 0) or 0
    g  = p.get('g', 0) or 0
    ip = p.get('ip', 0) or 0
    if gs > 0:
        raw = (ip - (g*1.5 - gs*1.5)) / gs
        return raw if raw > 0 else 5.0
    return 5.0

def get_pitcher_war162(p):
    """Port of JS getPitcherWar162()"""
    ip_start = get_ip_start(p)
    ip = p.get('ip', 0) or 0
    war = p.get('war', 0) or 0
    war_ip = war / ip if ip > 0 else 0
    return ip_start * war_ip * 32

def get_team_rotation_baseline(team, proj_pitchers):
    """Port of JS getTeamRotationBaseline() — top 5 starters by GS"""
    starters = [p for p in proj_pitchers if p.get('team') == team and (p.get('gs') or 0) > 0]
    starters.sort(key=lambda p: -(p.get('gs') or 0))
    return sum(get_pitcher_war162(p) for p in starters[:5])

def calc_all_proj_wins(proj_hitters, proj_pitchers, standings_gp):
    """
    Port of JS calcAllProjWins().
    standings_gp: dict of {team: games_played} from live standings
    """
    team_war = {t: 0.0 for t in TEAMS_30}
    for p in proj_hitters:
        t = p.get('team')
        if t in team_war: team_war[t] += p.get('war', 0) or 0
    for p in proj_pitchers:
        t = p.get('team')
        if t in team_war: team_war[t] += p.get('war', 0) or 0

    scaled_war = {}
    for t in TEAMS_30:
        gp = standings_gp.get(t, 0)
        g_rem = (162 - gp) if gp > 0 else 162
        if g_rem <= 0: g_rem = 1
        scaled_war[t] = team_war[t] * (162 / g_rem)

    vals = list(scaled_war.values())
    avg = sum(vals) / len(vals)
    return {t: 81 + (scaled_war[t] - avg) * 1.15 for t in TEAMS_30}

def find_pitcher(team, sp_name, proj_pitchers):
    """Find pitcher by name match"""
    if not sp_name: return None
    sp_norm = norm_name(sp_name)
    for p in proj_pitchers:
        if p.get('team') == team and norm_name(p.get('name','')) == sp_norm:
            return p
    # fuzzy: last name match
    last = sp_norm.split()[-1] if sp_norm.split() else sp_norm
    for p in proj_pitchers:
        if p.get('team') == team and last in norm_name(p.get('name','')):
            return p
    return None

def model_calc(aw, hw, aw_sp_name, hw_sp_name, proj_hitters, proj_pitchers,
               proj_wins, aw_lineup=None, hw_lineup=None):
    """
    Full port of JS modelCalc().
    aw_lineup / hw_lineup: list of dicts {name, war, pa, g} for confirmed batters
    Returns (aw_wp, hw_wp)
    """
    def stage_calc(team, sp_name, p_wins, lineup_players, is_home):
        # 1. SP WAR/162
        sp = find_pitcher(team, sp_name, proj_pitchers)
        sp_war162 = get_pitcher_war162(sp) if sp else 0.0

        # 2. Rotation baseline
        rotation = get_team_rotation_baseline(team, proj_pitchers)

        # 3. Pitcher value + change
        pitcher_value = sp_war162 * 5
        pitcher_change = pitcher_value - rotation

        # 4. Projected starters (confirmed lineup or top-9 by war)
        if lineup_players and len(lineup_players) >= 7:
            # Use confirmed/projected lineup
            proj_starters = sum(
                (p.get('g',0) > 0 and
                 (p.get('pa',0)/p.get('g',1)) * (p.get('war',0)/p.get('pa',1) if p.get('pa',0) > 0 else 0) * 162
                 or 0)
                for p in lineup_players[:9]
            ) * 1.08
        else:
            # Top-9 by war from projections
            team_h = [p for p in proj_hitters if p.get('team') == team and (p.get('war',0) or 0) > 0]
            team_h.sort(key=lambda p: -(p.get('war',0) or 0))
            top9 = team_h[:9]
            proj_starters = sum(
                (p.get('g',0) > 0 and
                 (p.get('pa',0)/p.get('g',1)) * (p.get('war',0)/p.get('pa',1) if p.get('pa',0) > 0 else 0) * 162
                 or 0)
                for p in top9
            ) * 1.08

        # 5. Total hitter WAR (team baseline, weighted avg × 9)
        live_hitters = [p for p in proj_hitters if p.get('team') == team]
        total_pa = sum(p.get('pa',0) or 0 for p in live_hitters)
        if total_pa > 0:
            total_hitter_war = sum(
                ((p.get('g',0) > 0 and
                  (p.get('pa',0)/p.get('g',1)) * (p.get('war',0)/p.get('pa',1) if p.get('pa',0) > 0 else 0) * 162
                  or 0) * ((p.get('pa',0) or 0) / total_pa))
                for p in live_hitters
            ) * 9
        else:
            total_hitter_war = 0

        hitter_change = proj_starters - total_hitter_war
        total_change  = pitcher_change + hitter_change
        wins    = p_wins + total_change
        win_pct = (wins / 162) * (1.07 if is_home else 1.0)
        return win_pct

    aw_win_pct = stage_calc(aw, aw_sp_name, proj_wins.get(aw, 81), aw_lineup, False)
    hw_win_pct = stage_calc(hw, hw_sp_name, proj_wins.get(hw, 81), hw_lineup, True)

    aw_gs = aw_win_pct * (1 - hw_win_pct)
    hw_gs = hw_win_pct * (1 - aw_win_pct)
    tot   = aw_gs + hw_gs
    if tot == 0: return 0.5, 0.5
    return aw_gs/tot, hw_gs/tot

# ── Fetch Steamer projections via Render proxy ────────────────────────────
def fetch_projections():
    base = RENDER_URL
    bat_url = f'{base}/fg-proj/?type=steamerr&stats=bat&pos=all&team=0&players=0&lg=all'
    pit_url = f'{base}/fg-proj/?type=steamerr&stats=pit&pos=all&team=0&players=0&lg=all'

    # First try Firestore snapshot cache (saves Render calls)
    try:
        today = today_et()
        snap = db.collection('proj_snapshots').document(today).get()
        if snap.exists:
            data = snap.to_dict()
            if data.get('bat') and data.get('pit'):
                print('Projections loaded from Firestore snapshot')
                bat = [{'name':r[0],'team':r[1],'g':r[2],'pa':r[3],'war':r[4]} for r in data['bat']]
                pit = [{'name':r[0],'team':r[1],'g':r[2],'gs':r[3],'ip':r[4],'war':r[5]} for r in data['pit']]
                return bat, pit
    except Exception as e:
        print(f'Snapshot cache miss: {e}')

    print('Fetching fresh Steamer projections from FanGraphs via Render...')
    bat_r = requests.get(bat_url, timeout=45)
    pit_r = requests.get(pit_url, timeout=45)
    if not bat_r.ok or not pit_r.ok:
        raise Exception(f'Projection fetch failed: {bat_r.status_code}/{pit_r.status_code}')

    bat_raw = bat_r.json()
    pit_raw = pit_r.json()

    hitters = [
        {'name': p.get('PlayerName') or p.get('Name',''),
         'team': fg_team(p.get('Team') or p.get('team','')),
         'g':    float(p.get('G') or 0),
         'pa':   float(p.get('PA') or 0),
         'war':  float(p.get('WAR') or 0)}
        for p in bat_raw if float(p.get('PA') or 0) >= 10
    ]
    pitchers = [
        {'name': p.get('PlayerName') or p.get('Name',''),
         'team': fg_team(p.get('Team') or p.get('team','')),
         'g':    float(p.get('G') or 0),
         'gs':   float(p.get('GS') or 0),
         'ip':   float(p.get('IP') or 0),
         'war':  float(p.get('WAR') or 0)}
        for p in pit_raw if float(p.get('IP') or 0) >= 5
    ]
    print(f'Projections: {len(hitters)} hitters, {len(pitchers)} pitchers')
    return hitters, pitchers

# ── Fetch standings GP from Firestore cache ───────────────────────────────
def fetch_standings_gp():
    try:
        snap = db.collection('auto_slate').document('standings_gp').get()
        if snap.exists:
            return snap.to_dict() or {}
    except: pass
    # Fetch fresh from MLB API
    try:
        url = 'https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season=2026&standingsTypes=regularSeason&hydrate=team'
        data = requests.get(url, timeout=15).json()
        gp = {}
        for div in data.get('records', []):
            for tr in div.get('teamRecords', []):
                tid = tr['team']['id']
                abr = MLB_ID_TO_ABR.get(tid, tr['team'].get('abbreviation',''))
                if abr: gp[abr] = (tr.get('wins',0) or 0) + (tr.get('losses',0) or 0)
        # Cache for 4 hours
        db.collection('auto_slate').document('standings_gp').set(
            {**gp, '_ts': int(time.time())}
        )
        print(f'Standings GP fetched: {len(gp)} teams')
        return gp
    except Exception as e:
        print(f'Standings fetch failed: {e}')
        return {}

# ── Bankroll from Firestore ───────────────────────────────────────────────
def get_bankroll():
    try:
        today = today_et()
        ymd   = today  # YYYY-MM-DD
        # Try today's confirmed daily balance first
        daily = db.collection('bankroll').document(f'daily_{ymd}').get()
        if daily.exists:
            d = daily.to_dict()
            if d.get('confirmed') and d.get('start'):
                return float(d['start']), 0.5, 3.0
        # Fall back to settings
        sett = db.collection('bankroll').document('settings').get()
        if sett.exists:
            d = sett.to_dict()
            return float(d.get('start',10000)), float(d.get('kelly',0.5)), float(d.get('maxPct',3.0))
    except Exception as e:
        print(f'Bankroll fetch failed: {e}')
    return 10000, 0.5, 3.0

# ── Dedup cache ───────────────────────────────────────────────────────────
def already_notified(date_str, team):
    try:
        return db.collection('tg_notified').document(f'{date_str}_{team}').get().exists
    except: return False

def mark_notified(date_str, team):
    try:
        db.collection('tg_notified').document(f'{date_str}_{team}').set(
            {'ts': firestore.SERVER_TIMESTAMP}
        )
    except: pass

# ── Telegram ──────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    r = requests.post(url, json={'chat_id':TG_CHAT,'text':text,'parse_mode':'Markdown'}, timeout=10)
    if not r.ok: print(f'Telegram error: {r.text}')

# ── Main ──────────────────────────────────────────────────────────────────
def run():
    date = today_et()
    print(f'Pipeline running for {date}')

    # ── Projections ───────────────────────────────────────────────────────
    try:
        proj_hitters, proj_pitchers = fetch_projections()
    except Exception as e:
        print(f'FATAL: Projections failed: {e}')
        return

    # ── Standings GP for proj wins scaling ───────────────────────────────
    standings_gp = fetch_standings_gp()

    # ── Compute projected wins dynamically (same as JS) ───────────────────
    proj_wins = calc_all_proj_wins(proj_hitters, proj_pitchers, standings_gp)
    print(f'Sample proj wins — NYY:{proj_wins.get("NYY",0):.1f} LAD:{proj_wins.get("LAD",0):.1f}')

    # ── MLB schedule ──────────────────────────────────────────────────────
    try:
        url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=lineups,probablePitcher(note)'
        mlb_games = requests.get(url, timeout=15).json().get('dates',[])
        if not mlb_games or not mlb_games[0].get('games'):
            print('No games today'); return
        mlb_games = mlb_games[0]['games']
        print(f'{len(mlb_games)} games found')
    except Exception as e:
        print(f'MLB API failed: {e}'); return

    # Build game objects
    games = []
    for g in mlb_games:
        aw_id = g['teams']['away']['team']['id']
        hw_id = g['teams']['home']['team']['id']
        aw = MLB_ID_TO_ABR.get(aw_id, g['teams']['away']['team'].get('abbreviation',''))
        hw = MLB_ID_TO_ABR.get(hw_id, g['teams']['home']['team'].get('abbreviation',''))
        if not aw or not hw: continue
        games.append({
            'id': g['gamePk'],
            'away': aw, 'home': hw,
            'game_time_utc': g.get('gameDate',''),
            'game_time_et':  fmt_time_et(g.get('gameDate','')),
            'aw_sp': g['teams']['away'].get('probablePitcher',{}).get('fullName',''),
            'hw_sp': g['teams']['home'].get('probablePitcher',{}).get('fullName',''),
            'aw_lineup': [], 'hw_lineup': [],
            'has_lineup': False,
            'aw_odds': None, 'hw_odds': None,
            'aw_novig': None, 'hw_novig': None,
        })

    # ── FanGraphs lineups ─────────────────────────────────────────────────
    try:
        fg_url = f'{RENDER_URL}/fg/live-all?gamedate={date}'
        fg_text = requests.get(fg_url, timeout=30).text
        if fg_text.strip().startswith('['):
            fg_data = json.loads(fg_text)
            for fg in fg_data:
                s = fg.get('schedule',{})
                aw_abr = FG_TEAM_MAP.get(s.get('AwayTeamAbbName',''), s.get('AwayTeamAbbName',''))
                hw_abr = FG_TEAM_MAP.get(s.get('HomeTeamAbbName',''), s.get('HomeTeamAbbName',''))
                gm = next((x for x in games if x['away']==aw_abr and x['home']==hw_abr), None)
                if not gm: continue

                lin = fg.get('lineups',{})
                gi  = lin.get('gameInfo',{})

                # SP — prefer PP (bulk pitcher in opener games)
                aw_pp = next((p for p in (lin.get('lineupAway') or []) if p.get('Position')=='PP'), None)
                hw_pp = next((p for p in (lin.get('lineupHome') or []) if p.get('Position')=='PP'), None)
                if aw_pp: gm['aw_sp'] = aw_pp.get('PlayerName', gm['aw_sp'])
                elif gi.get('ProbableAwayPlayerName'): gm['aw_sp'] = gi['ProbableAwayPlayerName']
                if hw_pp: gm['hw_sp'] = hw_pp.get('PlayerName', gm['hw_sp'])
                elif gi.get('ProbableHomePlayerName'): gm['hw_sp'] = gi['ProbableHomePlayerName']

                # Batting lineup — strip OP/PP
                aw_pl = sorted([p for p in (lin.get('lineupAway') or [])
                                if p.get('Position') not in ('OP','PP')],
                               key=lambda x: x.get('BatOrder',99))
                hw_pl = sorted([p for p in (lin.get('lineupHome') or [])
                                if p.get('Position') not in ('OP','PP')],
                               key=lambda x: x.get('BatOrder',99))

                if len(aw_pl) >= 7 and len(hw_pl) >= 7:
                    gm['has_lineup'] = True
                    # Map FG players to projection data for WAR
                    def map_players(players, team):
                        result = []
                        for p in players[:9]:
                            pname = p.get('PlayerName','')
                            pnorm = norm_name(pname)
                            match = next(
                                (h for h in proj_hitters
                                 if h.get('team')==team and norm_name(h.get('name',''))==pnorm),
                                None
                            )
                            if match:
                                result.append(match)
                            else:
                                result.append({'name':pname,'team':team,'g':150,'pa':500,'war':0})
                        return result
                    gm['aw_lineup'] = map_players(aw_pl, aw_abr)
                    gm['hw_lineup'] = map_players(hw_pl, hw_abr)
            print('FanGraphs lineups applied')
        else:
            print('FanGraphs: non-JSON (Render may be cold)')
    except Exception as e:
        print(f'FanGraphs failed: {e}')

    # ── BetOnline + NoVig odds ────────────────────────────────────────────
    try:
        odds_url = (f'https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/'
                    f'?apiKey={ODDS_API_KEY}&regions=us,us_ex&markets=h2h'
                    f'&oddsFormat=american&bookmakers=betonlineag,novig')
        odds_data = requests.get(odds_url, timeout=15).json()
        print(f'Odds: {len(odds_data)} events')
        for ev in odds_data:
            if get_et_date(ev['commence_time']) != date: continue
            aw_n = ev['away_team']; hw_n = ev['home_team']
            aw_a = NAME_TO_ABR.get(aw_n, aw_n.split()[-1][:3].upper())
            hw_a = NAME_TO_ABR.get(hw_n, hw_n.split()[-1][:3].upper())
            print(f'Odds event: {aw_n}({aw_a}) @ {hw_n}({hw_a})')
            gm = next((x for x in games if x['away']==aw_a and x['home']==hw_a), None)
            if not gm:
                print(f'  → NO GAME MATCH for {aw_a} @ {hw_a}')
                continue
            for bk in ev['bookmakers']:
                mkt = next((m for m in bk['markets'] if m['key']=='h2h'), None)
                if not mkt: continue
                ao = next((o for o in mkt['outcomes'] if o['name']==aw_n), None)
                ho = next((o for o in mkt['outcomes'] if o['name']==hw_n), None)
                if not (ao and ho): continue
                if bk['key'] == 'betonlineag':
                    gm['aw_odds'] = ao['price']; gm['hw_odds'] = ho['price']
                elif bk['key'] == 'novig':
                    gm['aw_novig'] = ao['price']; gm['hw_novig'] = ho['price']
    except Exception as e:
        print(f'Odds failed: {e}')

    # ── Model + edges ─────────────────────────────────────────────────────
    bankroll, kelly_frac, kelly_max = get_bankroll()
    alerts = []
    games_out = []

    for gm in games:
        aw, hw = gm['away'], gm['home']
        aw_wp, hw_wp = model_calc(
            aw, hw, gm['aw_sp'], gm['hw_sp'],
            proj_hitters, proj_pitchers, proj_wins,
            gm['aw_lineup'] if gm['has_lineup'] else None,
            gm['hw_lineup'] if gm['has_lineup'] else None,
        )
        gm['aw_wp'] = round(aw_wp*100, 2)
        gm['hw_wp'] = round(hw_wp*100, 2)
        gm['aw_mdl'] = p_to_american(aw_wp)
        gm['hw_mdl'] = p_to_american(hw_wp)
        print(f'{aw}@{hw} | lineup={gm["has_lineup"]} | {aw}_wp={aw_wp*100:.1f}% {hw}_wp={hw_wp*100:.1f}% | aw_odds={gm["aw_odds"]} hw_odds={gm["hw_odds"]} | aw_edge={round((aw_wp-impl(gm["aw_odds"]))*100,1) if gm["aw_odds"] else "—"}% hw_edge={round((hw_wp-impl(gm["hw_odds"]))*100,1) if gm["hw_odds"] else "—"}%')

        # Best odds (higher American = better for bettor)
        def best(nv, bo):
            if nv is None and bo is None: return None
            if nv is None: return bo
            if bo is None: return nv
            return nv if nv >= bo else bo

        aw_best = best(gm['aw_novig'], gm['aw_odds'])
        hw_best = best(gm['hw_novig'], gm['hw_odds'])
        gm['aw_edge'] = round((aw_wp - impl(aw_best))*100, 2) if aw_best else None
        gm['hw_edge'] = round((hw_wp - impl(hw_best))*100, 2) if hw_best else None

        # Alert on BO edge only — confirmed lineups only
        if not gm['has_lineup']: continue
        for team, opp, side, odds, wp, edge in [
            (aw, hw, 'away', gm['aw_odds'], aw_wp, gm['aw_edge']),
            (hw, aw, 'home', gm['hw_odds'], hw_wp, gm['hw_edge']),
        ]:
            if odds is None or edge is None or edge < EDGE_THRESHOLD*100: continue
            if already_notified(date, team): continue
            ku = calc_kelly(odds, edge)
            hk_pct = min(ku * kelly_frac, kelly_max)
            risk = round(hk_pct/100 * bankroll)
            win  = round(risk*odds/100) if odds>=0 else round(risk*100/abs(odds))
            ha   = '@' if side=='away' else 'vs'
            date_fmt = datetime.datetime.strptime(date,'%Y-%m-%d').strftime('%m/%d')
            alerts.append({
                'team':team,'opp':opp,'side':side,'odds':odds,'edge':edge,
                'wp':round(wp*100,1),'hk_pct':round(hk_pct,1),
                'risk':risk,'win':win,'ha':ha,'date_fmt':date_fmt,
                'game_time':gm['game_time_et'],'date':date,
            })

        games_out.append(gm)

    # ── Write to Firestore ────────────────────────────────────────────────
    try:
        db.collection('auto_slate').document(date).set({
            'games': games_out,
            'lastUpdated': firestore.SERVER_TIMESTAMP,
            'lastUpdatedMs': int(time.time()*1000),
            'date': date,
        })
        print(f'Wrote {len(games_out)} games to Firestore')
    except Exception as e:
        print(f'Firestore write failed: {e}')

    # ── Telegram alerts ───────────────────────────────────────────────────
    for a in alerts:
        text = (
            f"*Recommended Bet*\n"
            f"{a['team']} {fmt_odds(a['odds'])} {a['ha']} {a['opp']} | {a['game_time']} ({a['date_fmt']})\n"
            f"Edge: {a['edge']:.1f}% | Half-Kelly: {a['hk_pct']:.1f}%\n"
            f"Starting Bankroll: ${bankroll:,.0f}\n"
            f"Bet: Risk ${a['risk']:,} to win ${a['win']:,}"
        )
        send_telegram(text)
        mark_notified(a['date'], a['team'])
        print(f'Alert: {a["team"]} {fmt_odds(a["odds"])} edge={a["edge"]:.1f}%')

    print(f'Done. {len(alerts)} alerts sent.')

if __name__ == '__main__':
    run()
