#!/usr/bin/env python3
"""
MLB Auto-Pipeline — runs every 20 min via GitHub Actions
Fetches lineups + odds, runs model, writes to Firestore, sends Telegram alerts
"""

import os, json, math, requests, datetime
from zoneinfo import ZoneInfo

# ── Firebase Admin ───────────────────────────────────────────────────────
import firebase_admin
from firebase_admin import credentials, firestore

SA_JSON = os.environ.get('FIREBASE_SA_JSON', '')
cred = credentials.Certificate(json.loads(SA_JSON))
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Config ───────────────────────────────────────────────────────────────
RENDER_URL    = 'https://mlb-model-xwti.onrender.com'
ODDS_API_KEY  = '16381a969b24dad9592530007f8b51f9'
TG_TOKEN      = '8699040384:AAHGUYll31NgDPKFthDMB0_YTiKOVSOdF1A'
TG_CHAT       = '-5110059891'
ET            = ZoneInfo('America/New_York')
EDGE_THRESHOLD = 0.03   # 3%
KELLY_FRACTION = 0.5    # half-Kelly
KELLY_MAX_PCT  = 3.0    # max 3% of bankroll

# ── Team abbreviation mappings ────────────────────────────────────────────
FG_TO_ABR = {
    'SF':'SFG','SFG':'SFG','SD':'SDP','SDP':'SDP','KC':'KCR','KCR':'KCR',
    'TB':'TBR','TBR':'TBR','WAS':'WSN','WSH':'WSN','OAK':'ATH','ATH':'ATH',
    'CWS':'CHW','CHW':'CHW',
}
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

# ── Embedded model constants (from dashboard) ─────────────────────────────
PROJ_WINS = {
    'ARI':81.48,'ATH':77.26,'ATL':86.55,'BAL':86.40,'BOS':86.65,'CHC':83.04,
    'CHW':68.77,'CIN':75.88,'CLE':74.23,'COL':59.79,'DET':87.00,'HOU':83.55,
    'KCR':82.57,'LAA':72.05,'LAD':98.37,'MIA':73.51,'MIL':80.48,'MIN':78.89,
    'NYM':90.90,'NYY':90.78,'PHI':86.62,'PIT':77.40,'SDP':82.88,'SEA':88.82,
    'SFG':78.65,'STL':73.12,'TBR':80.03,'TEX':83.71,'TOR':90.91,'WSN':69.72,
}

# Load TEAM_DATA from dashboard JS — extract rotation_total and total_hitter_war
TEAM_DATA_RAW = {}  # populated below from embedded JSON

# ── Math helpers ──────────────────────────────────────────────────────────
def impl(odds):
    if odds >= 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)

def p_to_american(p):
    if p <= 0 or p >= 1:
        return 0
    if p < 0.5:
        return round((1 - p) / p * 100)
    else:
        return round(-p / (1 - p) * 100)

def calc_kelly(odds, edge_pct):
    if edge_pct <= 0:
        return 0
    dec = (odds / 100 + 1) if odds >= 0 else (100 / abs(odds) + 1)
    b = dec - 1
    if b <= 0:
        return 0
    imp = impl(odds)
    p = imp + edge_pct / 100
    q = 1 - p
    kelly = (b * p - q) / b
    return max(0, kelly) * 100

def get_team_data(abr):
    """Load rotation_total and total_hitter_war from Firestore team_data cache or use hardcoded"""
    # Use the constants from the dashboard
    team_data = {
        'ARI':{'rotation_total':10.69,'total_hitter_war':26.74},
        'ATH':{'rotation_total':9.28,'total_hitter_war':24.64},
        'ATL':{'rotation_total':13.63,'total_hitter_war':26.55},
        'BAL':{'rotation_total':12.02,'total_hitter_war':27.67},
        'BOS':{'rotation_total':17.83,'total_hitter_war':21.71},
        'CHC':{'rotation_total':10.70,'total_hitter_war':26.20},
        'CHW':{'rotation_total':7.91,'total_hitter_war':16.58},
        'CIN':{'rotation_total':11.13,'total_hitter_war':18.51},
        'CLE':{'rotation_total':9.45,'total_hitter_war':21.15},
        'COL':{'rotation_total':6.38,'total_hitter_war':12.01},
        'DET':{'rotation_total':17.14,'total_hitter_war':22.23},
        'HOU':{'rotation_total':10.72,'total_hitter_war':26.75},
        'KCR':{'rotation_total':12.07,'total_hitter_war':24.54},
        'LAA':{'rotation_total':12.39,'total_hitter_war':10.29},
        'LAD':{'rotation_total':17.05,'total_hitter_war':32.16},
        'MIA':{'rotation_total':12.49,'total_hitter_war':17.49},
        'MIL':{'rotation_total':10.82,'total_hitter_war':15.60},
        'MIN':{'rotation_total':11.13,'total_hitter_war':21.16},
        'NYM':{'rotation_total':12.29,'total_hitter_war':30.33},
        'NYY':{'rotation_total':13.48,'total_hitter_war':23.57},
        'PHI':{'rotation_total':17.22,'total_hitter_war':23.50},
        'PIT':{'rotation_total':13.84,'total_hitter_war':18.68},
        'SDP':{'rotation_total':10.65,'total_hitter_war':25.21},
        'SEA':{'rotation_total':14.31,'total_hitter_war':29.46},
        'SFG':{'rotation_total':10.45,'total_hitter_war':23.76},
        'STL':{'rotation_total':7.66,'total_hitter_war':22.47},
        'TBR':{'rotation_total':14.65,'total_hitter_war':18.24},
        'TEX':{'rotation_total':14.97,'total_hitter_war':18.18},
        'TOR':{'rotation_total':13.73,'total_hitter_war':26.22},
        'WSN':{'rotation_total':9.91,'total_hitter_war':13.04},
    }
    return team_data.get(abr, {'rotation_total':10.0,'total_hitter_war':20.0})

def model_calc(aw, hw, aw_sp_war162, hw_sp_war162, aw_lineup_war=None, hw_lineup_war=None):
    """
    Port of the JS modelCalc function.
    Returns (aw_wp, hw_wp) win probabilities.
    """
    aw_td = get_team_data(aw)
    hw_td = get_team_data(hw)
    aw_base = PROJ_WINS.get(aw, 81)
    hw_base = PROJ_WINS.get(hw, 81)

    def stage_calc(team, td, sp_war162, proj_wins, lineup_war, is_home):
        rotation = td['rotation_total']
        total_hitter_war = td['total_hitter_war']

        # proj_starters = lineup WAR × 9 × 1.08 (or team baseline × 9)
        proj_starters = (lineup_war * 9 * 1.08) if lineup_war else (total_hitter_war / 9 * 9 * 1.08)
        pitcher_value = sp_war162 * 5
        pitcher_change = pitcher_value - rotation
        hitter_change = proj_starters - total_hitter_war

        total_change = pitcher_change + hitter_change
        wins = proj_wins + total_change
        win_pct = (wins / 162) * (1.07 if is_home else 1.0)
        return win_pct

    aw_wp = stage_calc(aw, aw_td, aw_sp_war162, aw_base, aw_lineup_war, False)
    hw_wp = stage_calc(hw, hw_td, hw_sp_war162, hw_base, hw_lineup_war, True)

    aw_game_score = aw_wp * (1 - hw_wp)
    hw_game_score = hw_wp * (1 - aw_wp)
    tot = aw_game_score + hw_game_score
    if tot == 0:
        return 0.5, 0.5
    return aw_game_score / tot, hw_game_score / tot


def get_et_date(utc_str):
    d = datetime.datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
    return d.astimezone(ET).strftime('%Y-%m-%d')

def fg_abr(s):
    return FG_TO_ABR.get(s, s)

def today_et():
    return datetime.datetime.now(ET).strftime('%Y-%m-%d')

def fmt_odds(od):
    return f'+{od}' if od >= 0 else str(od)

def fmt_time_et(utc_str):
    try:
        d = datetime.datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        return d.astimezone(ET).strftime('%-I:%M %p ET')
    except:
        return ''


# ── Fetch bankroll from Firestore ─────────────────────────────────────────
def get_bankroll():
    try:
        br_doc = db.collection('bankroll').document('settings').get()
        if br_doc.exists:
            br = br_doc.to_dict()
            start = br.get('start', 10000)
            kelly = br.get('kelly', 0.5)
            max_pct = br.get('maxPct', 3.0)
            return start, kelly, max_pct
    except Exception as e:
        print(f'Bankroll fetch failed: {e}')
    return 10000, 0.5, 3.0


# ── Already-notified cache in Firestore ───────────────────────────────────
def already_notified(date_str, team):
    try:
        doc = db.collection('tg_notified').document(f'{date_str}_{team}').get()
        return doc.exists
    except:
        return False

def mark_notified(date_str, team):
    try:
        db.collection('tg_notified').document(f'{date_str}_{team}').set({'ts': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        print(f'Mark notified failed: {e}')


# ── Send Telegram message ─────────────────────────────────────────────────
def send_telegram(text):
    url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    resp = requests.post(url, json={
        'chat_id': TG_CHAT,
        'text': text,
        'parse_mode': 'Markdown'
    }, timeout=10)
    if not resp.ok:
        print(f'Telegram error: {resp.text}')


# ── Main pipeline ─────────────────────────────────────────────────────────
def run():
    date = today_et()
    print(f'Pipeline running for {date}')

    # ── Step 1: MLB schedule ──────────────────────────────────────────────
    try:
        url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=lineups,probablePitcher(note)'
        games_raw = requests.get(url, timeout=15).json()
        dates = games_raw.get('dates', [])
        if not dates or not dates[0].get('games'):
            print('No games today')
            return
        mlb_games = dates[0]['games']
        print(f'Found {len(mlb_games)} games')
    except Exception as e:
        print(f'MLB API failed: {e}')
        return

    # Build game objects
    games = []
    for g in mlb_games:
        aw_id = g['teams']['away']['team']['id']
        hw_id = g['teams']['home']['team']['id']
        aw = MLB_ID_TO_ABR.get(aw_id, g['teams']['away']['team'].get('abbreviation', ''))
        hw = MLB_ID_TO_ABR.get(hw_id, g['teams']['home']['team'].get('abbreviation', ''))
        if not aw or not hw:
            continue  # skip if we can't identify the team
        game_time_utc = g.get('gameDate', '')
        # Probable pitchers from MLB API
        aw_pp = g['teams']['away'].get('probablePitcher', {}).get('fullName', '')
        hw_pp = g['teams']['home'].get('probablePitcher', {}).get('fullName', '')
        # Confirmed lineup
        lineups = g.get('lineups', {})
        aw_lineup = lineups.get('awayPlayers', [])
        hw_lineup = lineups.get('homePlayers', [])
        has_lineup = len(aw_lineup) >= 8 and len(hw_lineup) >= 8
        games.append({
            'id': g['gamePk'],
            'away': aw, 'home': hw,
            'game_time_utc': game_time_utc,
            'game_time_et': fmt_time_et(game_time_utc),
            'aw_sp_name': aw_pp, 'hw_sp_name': hw_pp,
            'aw_sp_war162': 0, 'hw_sp_war162': 0,
            'aw_lineup': aw_lineup, 'hw_lineup': hw_lineup,
            'has_lineup': has_lineup,
            'aw_odds': None, 'hw_odds': None,
            'aw_novig': None, 'hw_novig': None,
            'aw_wp': None, 'hw_wp': None,
        })

    # ── Step 2: FanGraphs lineups via Render proxy ────────────────────────
    try:
        fg_url = f'{RENDER_URL}/fg/live-all?gamedate={date}'
        fg_resp = requests.get(fg_url, timeout=30)
        fg_text = fg_resp.text
        if fg_text.strip().startswith('['):
            fg_data = json.loads(fg_text)
            # Match FG games to our games
            for fg_game in fg_data:
                sched = fg_game.get('schedule', {})
                aw_abr = fg_abr(sched.get('AwayTeamAbbName', ''))
                hw_abr = fg_abr(sched.get('HomeTeamAbbName', ''))
                g = next((x for x in games if x['away'] == aw_abr and x['home'] == hw_abr), None)
                if not g:
                    continue
                lineups = fg_game.get('lineups', {})
                gi = lineups.get('gameInfo', {})
                aw_players = sorted([p for p in (lineups.get('lineupAway') or [])
                                     if p.get('Position') not in ('OP','PP')],
                                    key=lambda x: x.get('BatOrder', 99))
                hw_players = sorted([p for p in (lineups.get('lineupHome') or [])
                                     if p.get('Position') not in ('OP','PP')],
                                    key=lambda x: x.get('BatOrder', 99))
                # PP = probable pitcher (bulk pitcher in opener games)
                aw_pp_fg = next((p for p in (lineups.get('lineupAway') or []) if p.get('Position') == 'PP'), None)
                hw_pp_fg = next((p for p in (lineups.get('lineupHome') or []) if p.get('Position') == 'PP'), None)
                aw_sp_name = (aw_pp_fg or {}).get('PlayerName') or gi.get('ProbableAwayPlayerName') or g['aw_sp_name']
                hw_sp_name = (hw_pp_fg or {}).get('PlayerName') or gi.get('ProbableHomePlayerName') or g['hw_sp_name']
                g['aw_sp_name'] = aw_sp_name
                g['hw_sp_name'] = hw_sp_name
                if len(aw_players) >= 7 and len(hw_players) >= 7:
                    g['has_lineup'] = True
                    g['fg_aw_players'] = aw_players
                    g['fg_hw_players'] = hw_players
            print(f'FanGraphs lineups applied')
        else:
            print('FanGraphs: non-JSON response (Render may be cold)')
    except Exception as e:
        print(f'FanGraphs fetch failed: {e}')

    # ── Step 3: Look up SP WAR from Firestore proj_pitchers cache ─────────
    # Simple fallback: use rotation_sp from TEAM_DATA hardcoded above
    # (same data the dashboard uses)
    ROTATION_SP = {
        'ARI': [('Zac Gallen',2.27),('Merrill Kelly',2.56),('Brandon Pfaadt',2.02),('Ryne Nelson',1.75),('Eduardo Rodriguez',2.09)],
        'ATH': [('Luis Severino',1.98),('Jeffrey Springs',1.96),('Aaron Civale',1.70),('Jacob Lopez',2.08),('Luis Morales',1.58)],
        'ATL': [('Chris Sale',4.82),('Spencer Strider',3.10),('Reynaldo López',2.36),('Grant Holmes',1.79),('Bryce Elder',1.57)],
        'BAL': [('Chris Bassitt',2.09),('Shane Baz',2.14),('Kyle Bradish',3.55),('Trevor Rogers',2.35),('Zach Eflin',1.89)],
        'BOS': [('Garrett Crochet',5.59),('Sonny Gray',4.05),('Ranger Suarez',3.57),('Brayan Bello',2.14),('Johan Oviedo',2.48)],
        'CHC': [('Matthew Boyd',2.46),('Jameson Taillon',1.87),('Shota Imanaga',2.39),('Edward Cabrera',2.36),('Cade Horton',1.63)],
        'CHW': [('Shane Smith',1.90),('Davis Martin',1.57),('Sean Burke',1.05),('Anthony Kay',2.37),('Erick Fedde',1.02)],
        'CIN': [('Brady Singer',1.25),('Andrew Abbott',2.24),('Nick Lodolo',2.85),('Rhett Lowder',1.26),('Chase Burns',3.52)],
        'CLE': [('Tanner Bibee',2.40),('Gavin Williams',2.06),('Slade Cecconi',1.35),('Parker Messick',1.85),('Joey Cantillo',1.79)],
        'COL': [('Kyle Freeland',1.58),('Michael Lorenzen',1.37),('Jose Quintana',1.30),('Chase Dollander',0.89),('Tomoyuki Sugano',1.24)],
        'DET': [('Tarik Skubal',5.88),('Framber Valdez',3.88),('Jack Flaherty',2.85),('Casey Mize',2.50),('Justin Verlander',2.03)],
        'HOU': [('Hunter Brown',3.73),('Cristian Javier',1.01),('Tatsuya Imai',2.17),('Mike Burrows',2.02),('Lance McCullers Jr.',1.79)],
        'KCR': [('Cole Ragans',4.47),('Michael Wacha',2.13),('Seth Lugo',2.02),('Noah Cameron',1.93),('Kris Bubic',1.52)],
        'LAA': [('José Soriano',3.15),('Yusei Kikuchi',2.67),('Reid Detmers',2.43),('Grayson Rodriguez',3.23),('Tyler Anderson',0.91)],
        'LAD': [('Yoshinobu Yamamoto',4.25),('Tyler Glasnow',2.65),('Blake Snell',3.72),('Emmet Sheehan',2.81),('Shohei Ohtani',3.62)],
        'MIA': [('Sandy Alcantara',2.61),('Eury Pérez',3.04),('Max Meyer',2.38),('Braxton Garrett',2.89),('Chris Paddack',1.58)],
        'MIL': [('Brandon Woodruff',3.59),('Chad Patrick',1.65),('Jacob Misiorowski',1.95),('Quinn Priester',1.96),('Kyle Harrison',1.67)],
        'MIN': [('Joe Ryan',3.65),('Bailey Ober',2.33),('Simeon Woods Richardson',1.75),('Taj Bradley',2.28),('Mick Abel',1.12)],
        'NYM': [('Freddy Peralta',3.02),('David Peterson',2.41),('Clay Holmes',2.19),('Nolan McLean',2.51),('Kodai Senga',2.17)],
        'NYY': [('Max Fried',3.85),('Carlos Rodón',2.65),('Gerrit Cole',2.81),('Cam Schlittler',2.26),('Will Warren',1.91)],
        'PHI': [('Cristopher Sánchez',4.71),('Jesús Luzardo',3.66),('Aaron Nola',3.08),('Zack Wheeler',4.59),('Andrew Painter',1.19)],
        'PIT': [('Paul Skenes',4.22),('Mitch Keller',2.37),('Bubba Chandler',2.19),('Braxton Ashcraft',2.11),('Jared Jones',2.95)],
        'SDP': [('Michael King',3.20),('Nick Pivetta',2.88),('Joe Musgrove',2.70),('Randy Vásquez',0.80),('Germán Márquez',1.07)],
        'SEA': [('Bryan Woo',3.31),('George Kirby',3.30),('Luis Castillo',2.48),('Logan Gilbert',3.50),('Bryce Miller',1.72)],
        'SFG': [('Logan Webb',4.12),('Robbie Ray',1.70),('Tyler Mahle',1.71),('Adrian Houser',1.37),('Landen Roupp',1.55)],
        'STL': [('Matthew Liberatore',1.33),('Dustin May',1.90),('Andre Pallante',1.72),('Michael McGreevy',1.85),('Richard Fitts',0.86)],
        'TBR': [('Drew Rasmussen',3.37),('Ryan Pepiot',2.58),('Shane McClanahan',4.26),('Steven Matz',2.61),('Nick Martinez',1.83)],
        'TEX': [('Jacob deGrom',4.44),('Nathan Eovaldi',3.68),('MacKenzie Gore',2.98),('Jack Leiter',1.83),('Kumar Rocker',2.05)],
        'TOR': [('Dylan Cease',3.91),('Kevin Gausman',3.05),('Cody Ponce',2.21),('Trey Yesavage',1.53),('Shane Bieber',3.03)],
        'WSN': [('Cade Cavalli',2.71),('Zack Littell',1.83),('Miles Mikolas',1.42),('Foster Griffin',2.67),('Jake Irvin',1.30)],
    }

    def get_sp_war162(team, sp_name):
        if not sp_name:
            # use rotation average
            rsp = ROTATION_SP.get(team, [])
            return sum(w for _,w in rsp) / len(rsp) if rsp else 2.0
        rsp = ROTATION_SP.get(team, [])
        # fuzzy match by last name
        sp_lower = sp_name.lower()
        for name, war in rsp:
            if any(part in name.lower() for part in sp_lower.split()):
                return war
        return sum(w for _,w in rsp) / len(rsp) if rsp else 2.0

    # ── Step 4: BetOnline + NoVig odds ────────────────────────────────────
    try:
        odds_url = (f'https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/'
                    f'?apiKey={ODDS_API_KEY}&regions=us,us_ex&markets=h2h'
                    f'&oddsFormat=american&bookmakers=betonlineag,novig')
        odds_data = requests.get(odds_url, timeout=15).json()
        print(f'Odds API: {len(odds_data)} events')

        for event in odds_data:
            et_date = get_et_date(event['commence_time'])
            if et_date != date:
                continue
            aw_name = event['away_team']
            hw_name = event['home_team']
            aw_abr = NAME_TO_ABR.get(aw_name, aw_name.split()[-1][:3].upper())
            hw_abr = NAME_TO_ABR.get(hw_name, hw_name.split()[-1][:3].upper())
            g = next((x for x in games if x['away'] == aw_abr and x['home'] == hw_abr), None)
            if not g:
                continue
            for bk in event['bookmakers']:
                mkt = next((m for m in bk['markets'] if m['key'] == 'h2h'), None)
                if not mkt:
                    continue
                ao = next((o for o in mkt['outcomes'] if o['name'] == aw_name), None)
                ho = next((o for o in mkt['outcomes'] if o['name'] == hw_name), None)
                if not (ao and ho):
                    continue
                if bk['key'] == 'betonlineag':
                    g['aw_odds'] = ao['price']
                    g['hw_odds'] = ho['price']
                elif bk['key'] == 'novig':
                    g['aw_novig'] = ao['price']
                    g['hw_novig'] = ho['price']
    except Exception as e:
        print(f'Odds API failed: {e}')

    # ── Step 5: Run model + compute edges ─────────────────────────────────
    bankroll, kelly_frac, kelly_max = get_bankroll()

    alerts = []
    games_out = []

    for g in games:
        aw, hw = g['away'], g['home']
        aw_sp_war = get_sp_war162(aw, g['aw_sp_name'])
        hw_sp_war = get_sp_war162(hw, g['hw_sp_name'])
        g['aw_sp_war162'] = aw_sp_war
        g['hw_sp_war162'] = hw_sp_war

        aw_wp, hw_wp = model_calc(aw, hw, aw_sp_war, hw_sp_war)
        g['aw_wp'] = round(aw_wp * 100, 2)
        g['hw_wp'] = round(hw_wp * 100, 2)

        # Best odds = higher of novig/bo for edge calc
        def best_odds(nv, bo):
            if nv is None and bo is None: return None
            if nv is None: return bo
            if bo is None: return nv
            return nv if nv >= bo else bo

        aw_best = best_odds(g['aw_novig'], g['aw_odds'])
        hw_best = best_odds(g['hw_novig'], g['hw_odds'])

        aw_edge = (aw_wp - impl(aw_best)) if aw_best is not None else None
        hw_edge = (hw_wp - impl(hw_best)) if hw_best is not None else None
        g['aw_edge'] = round(aw_edge * 100, 2) if aw_edge is not None else None
        g['hw_edge'] = round(hw_edge * 100, 2) if hw_edge is not None else None
        g['aw_mdl'] = p_to_american(aw_wp)
        g['hw_mdl'] = p_to_american(hw_wp)

        # Check for alert-worthy edges using BO odds only
        for side, team, opp, odds, edge, wp in [
            ('away', aw, hw, g['aw_odds'], aw_edge, aw_wp),
            ('home', hw, aw, g['hw_odds'], hw_edge, hw_wp),
        ]:
            if odds is None or edge is None:
                continue
            if edge >= EDGE_THRESHOLD:
                if not already_notified(date, team):
                    ku = calc_kelly(odds, edge * 100)
                    half_kelly_pct = min(ku * kelly_frac, kelly_max)
                    risk_amt = round(half_kelly_pct / 100 * bankroll)
                    win_amt = round(risk_amt * odds / 100) if odds >= 0 else round(risk_amt * 100 / abs(odds))
                    alerts.append({
                        'team': team, 'opp': opp, 'side': side,
                        'odds': odds, 'edge': edge, 'wp': wp,
                        'half_kelly_pct': half_kelly_pct,
                        'risk_amt': risk_amt, 'win_amt': win_amt,
                        'game_time': g['game_time_et'],
                        'date': date,
                    })

        games_out.append(g)

    # ── Step 6: Write to Firestore ─────────────────────────────────────────
    try:
        import time as _time
        db.collection('auto_slate').document(date).set({
            'games': games_out,
            'lastUpdated': firestore.SERVER_TIMESTAMP,
            'lastUpdatedMs': int(_time.time() * 1000),
            'date': date,
        })
        print(f'Wrote {len(games_out)} games to Firestore auto_slate/{date}')
    except Exception as e:
        print(f'Firestore write failed: {e}')

    # ── Step 7: Send Telegram alerts ──────────────────────────────────────
    for a in alerts:
        ha = '@' if a['side'] == 'away' else 'vs'
        date_fmt = datetime.datetime.strptime(a['date'], '%Y-%m-%d').strftime('%m/%d')
        text = (
            f"*Recommended Bet*\n"
            f"{a['team']} {fmt_odds(a['odds'])} {ha} {a['opp']} | {a['game_time']} ({date_fmt})\n"
            f"Edge: {a['edge']*100:.1f}% | Half-Kelly: {a['half_kelly_pct']:.1f}%\n"
            f"Starting Bankroll: ${bankroll:,.0f}\n"
            f"Bet: Risk ${a['risk_amt']:,} to win ${a['win_amt']:,}"
        )
        send_telegram(text)
        mark_notified(a['date'], a['team'])
        print(f'Telegram sent: {a["team"]} {fmt_odds(a["odds"])} edge={a["edge"]*100:.1f}%')

    print(f'Pipeline complete. {len(alerts)} alerts sent.')

if __name__ == '__main__':
    run()
