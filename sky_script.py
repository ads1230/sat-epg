import requests
import os
import sys
import re
import html
import json
import concurrent.futures
import time
import random
from datetime import datetime, timedelta, timezone

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")
    sys.stdout.flush()

# --- Configuration ---
DAYS = 7 
FETCH_HOURS = range(24)  
LOGO_DIR = "logos_sky"
CACHE_FILE = "sky_cache.json"

# All Sky UK Regions mapped from the API
REGIONS = {
    "Anglia": "anglia", "Cambridgeshire": "cambridgeshire", "Channel Islands": "channel-islands",
    "Cumbria": "cumbria", "East Midlands": "east-midlands", "Henley on Thames": "henley-on-thames",
    "London": "london", "London (Essex)": "london-essex", "London (Kent)": "london-kent",
    "London (Thames Valley)": "london-thames-valley", "Meridian (East)": "meridian-east",
    "Meridian (West)": "meridian-west", "North East": "north-east", "North East Midlands": "north-east-midlands",
    "North West": "north-west", "North Yorkshire": "north-yorkshire", "Northern Ireland": "northern-ireland",
    "Oxford": "oxford", "Republic of Ireland": "republic-of-ireland", "Scotland (Borders)": "scotland-borders",
    "Scotland (Central)": "scotland-central", "Scotland (North)": "scotland-north", "South Lakeland": "south-lakeland",
    "Wales": "wales", "West Dorset": "west-dorset", "West England": "west-england", "West Midlands": "west-midlands",
    "Yorkshire": "yorkshire", "Yorkshire & Lincolnshire": "yorkshire-and-lincolnshire"
}

GITHUB_REPO_FULL = os.getenv('GITHUB_REPOSITORY', 'YourUsername/YourRepo')
GITHUB_USER, GITHUB_REPO = GITHUB_REPO_FULL.split('/') if '/' in GITHUB_REPO_FULL else ("Unknown", "Unknown")
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/{LOGO_DIR}/"

UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]

def clean_xml_text(text):
    if not text: return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]', "", str(text))

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: pass
    return {}

def fetch_sky_meta(pid, session):
    try:
        r = session.get(f"https://api-2.tvguide.co.uk/single?pa_id={pid}", timeout=15)
        if r.status_code == 200:
            d = r.json()
            m = d.get('meta', {})
            attrs = m.get('attributes', [])
            cats = m.get('categories', [])
            
            # Extract Actors and Directors from the contributor list
            contributors = m.get('contributor', [])
            actors = [c['name'] for c in contributors if c.get('role', '').lower() == 'actor']
            directors = [c['name'] for c in contributors if c.get('role', '').lower() == 'director']
            
            return pid, {
                'desc': d.get('summary_long') or d.get('summary_short', ''),
                'sub': m.get('episode_title', ''),
                'sn': m.get('season'),
                'en': m.get('episode'),
                'ad': 'audio-description' in attrs,
                'subs': 'subtitles' in attrs,
                'genre': d.get('genre', ''),
                'cats': cats,
                'actors': actors,
                'directors': directors
            }, 200
        return pid, {}, r.status_code
    except Exception as e: return pid, {}, str(e)

def run(target_region=None):
    if not os.path.exists(LOGO_DIR): os.makedirs(LOGO_DIR)
    meta_cache = load_cache()
    now_utc = datetime.now(timezone.utc)
    start_of_today = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)

    items = [(target_region, REGIONS[target_region])] if target_region in REGIONS else REGIONS.items()

    for region_name, nid in items:
        log(f"--- REGION: {region_name} (Sky UK) ---")
        
        session = requests.Session()
        session.headers.update({'User-Agent': random.choice(UAS)})
        
        channels, progs = {}, []
        missing_pids, missing_logos = {}, {}
        seen_pids = set() # Prevents duplicate shows from overlapping hour blocks

        # PASS 1: Build Schedule & Grab Channels (PARALLEL PER DAY)
        for day in range(DAYS):
            target_date = start_of_today + timedelta(days=day)
            date_str = target_date.strftime("%Y-%m-%d")
            
            grid_tasks = {}
            for h in FETCH_HOURS:
                url = f"https://api-2.tvguide.co.uk/listings?platform=sky&region={nid}&view=grid&date={date_str}&hour={h}&details=true"
                grid_tasks[url] = h

            total_grids = len(grid_tasks)
            completed_grids = 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                future_to_url = {executor.submit(session.get, url, timeout=15): url for url in grid_tasks}
                for future in concurrent.futures.as_completed(future_to_url):
                    url = future_to_url[future]
                    h = grid_tasks[url]
                    completed_grids += 1
                    
                    try:
                        r = future.result()
                        if r.status_code != 200:
                            log(f"   [ERROR] Pass 1 Failed on Day {day+1} Hour {h}: HTTP {r.status_code}")
                            continue
                        
                        data = r.json()
                        
                        for chan in data:
                            cid = str(chan.get('pa_id'))
                            if not cid: continue
                            
                            channels[cid] = {'name': chan.get('title', 'Unknown'), 'lcn': str(chan.get('epg', ''))}
                            
                            logo_url = chan.get('logo_url')
                            if logo_url:
                                logo_path = os.path.join(LOGO_DIR, f"{cid}.png")
                                if not os.path.exists(logo_path):
                                    missing_logos[cid] = (logo_path, logo_url)
                                    
                            for ev in chan.get('schedules', []):
                                pid = ev.get('pa_id')
                                start_str = ev.get('start_at')
                                duration_mins = ev.get('duration')
                                
                                if not pid or not start_str or duration_mins is None: continue
                                
                                if pid in seen_pids: continue
                                seen_pids.add(pid)
                                
                                try:
                                    start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                                    end_dt = start_dt + timedelta(minutes=int(duration_mins))
                                    
                                    s_time = start_dt.strftime('%Y%m%d%H%M%S +0000')
                                    e_time = end_dt.strftime('%Y%m%d%H%M%S +0000')
                                    
                                    if pid not in meta_cache:
                                        missing_pids[pid] = pid
                                        
                                    progs.append({
                                        'cid': cid, 'pid': pid, 't': ev.get('title', 'Unknown'),
                                        'img': ev.get('image_url', ''), 's': s_time, 'e': e_time
                                    })
                                except Exception: pass

                    except Exception as e: log(f"   [CRITICAL] Error parsing day {day+1} hour {h}: {e}")

                    # Daily Progress Bar
                    update_iv = max(1, total_grids // 4)
                    if completed_grids % update_iv == 0 or completed_grids == total_grids:
                        pct = completed_grids / total_grids
                        bar_len = 20
                        filled = int(bar_len * pct)
                        bar = '█' * filled + '-' * (bar_len - filled)
                        sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}]    Day {day+1} ({date_str}): parsing hourly EPG [{bar}] {pct*100:.1f}% ({completed_grids}/{total_grids})")
                        sys.stdout.flush()
            print() # Print a new line after the daily progress bar completes

        # PASS 1.5: Download Logos
        total_logos = len(missing_logos)
        if total_logos > 0:
            log(f"   [INFO] Found {total_logos} missing channel logos. Downloading...")
            completed = 0
            for cid, (path, url) in missing_logos.items():
                try:
                    img_data = session.get(url, timeout=10).content
                    with open(path, 'wb') as handler: handler.write(img_data)
                except Exception: pass
                
                completed += 1
                update_iv = max(1, total_logos // 10)
                if completed % update_iv == 0 or completed == total_logos:
                    pct = completed / total_logos
                    bar_len = 20
                    filled = int(bar_len * pct)
                    bar = '█' * filled + '-' * (bar_len - filled)
                    sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}]    Logo Progress: [{bar}] {pct*100:.1f}% ({completed}/{total_logos})")
                    sys.stdout.flush()
            print()
        else:
            log("   [INFO] All channel logos are already up to date.")

        # PASS 2: Metadata
        total_missing_list = list(missing_pids.items())
        total_to_fetch = len(total_missing_list)
        
        if total_to_fetch > 0:
            log(f"FETCHING {total_to_fetch} metadata items...")
            completed, success_count, blocked_count = 0, 0, 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(fetch_sky_meta, pid, session) for pid, _ in total_missing_list]
                for f in concurrent.futures.as_completed(futures):
                    pid, m_data, status = f.result()
                    completed += 1
                    
                    if status == 200:
                        meta_cache[pid] = m_data
                        success_count += 1
                    elif status == 404:
                        meta_cache[pid] = {}
                        success_count += 1
                    elif status in [403, 429]: blocked_count += 1

                    update_iv = max(1, total_to_fetch // 20)
                    if completed % update_iv == 0 or completed == total_to_fetch:
                        pct = completed / total_to_fetch
                        bar_len = 20
                        filled = int(bar_len * pct)
                        bar = '█' * filled + '-' * (bar_len - filled)
                        sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}]    Progress: [{bar}] {pct*100:.1f}% ({completed}/{total_to_fetch}) | Success: {success_count} | Blocks: {blocked_count}")
                        sys.stdout.flush()
                    
                    if blocked_count >= 5:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
            print()

            # --- SMART CACHE PRUNING (90MB TARGET) ---
            MAX_BYTES = 90 * 1024 * 1024 
            while True:
                cache_str = json.dumps(meta_cache, separators=(',', ':'))
                cache_size = len(cache_str.encode('utf-8'))
                if cache_size <= MAX_BYTES: break
                items_to_remove = max(1000, len(meta_cache) // 20)
                meta_cache = dict(list(meta_cache.items())[items_to_remove:])
                log(f"   [CACHE WARNING] Size hit {cache_size / (1024*1024):.1f}MB. Pruned oldest {items_to_remove} items.")

            with open(CACHE_FILE, 'w', encoding='utf-8') as f: f.write(cache_str)

        # PASS 3: Generate XML
        output_file = f"sky_{region_name.lower().replace(' ', '_').replace('(', '').replace(')', '')}.xml"
        log(f"Writing {output_file}...")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?><tv>\n')
            for cid, info in channels.items():
                f.write(f'  <channel id="{cid}">\n')
                f.write(f'    <display-name>{html.escape(info["name"])}</display-name>\n')
                if info.get('lcn'): f.write(f'    <lcn>{info["lcn"]}</lcn>\n')
                if os.path.exists(os.path.join(LOGO_DIR, f"{cid}.png")):
                    f.write(f'    <icon src="{GITHUB_RAW_BASE}{cid}.png" />\n')
                f.write(f'  </channel>\n')
                
            for p in progs:
                m = meta_cache.get(p['pid'], {})
                f.write(f'  <programme start="{p["s"]}" stop="{p["e"]}" channel="{p["cid"]}">\n')
                f.write(f'    <title>{html.escape(clean_xml_text(p["t"]))}</title>\n')
                if m.get('sub'): f.write(f'    <sub-title>{html.escape(clean_xml_text(m["sub"]))}</sub-title>\n')
                
                desc = clean_xml_text(m.get('desc', ''))
                if m.get('ad'): desc = f"[AD] {desc}" if desc else "[AD]"
                if desc: f.write(f'    <desc>{html.escape(desc)}</desc>\n')
                
                if m.get('actors') or m.get('directors'):
                    f.write('    <credits>\n')
                    for d in m.get('directors', []): f.write(f'      <director>{html.escape(clean_xml_text(d))}</director>\n')
                    for a in m.get('actors', []): f.write(f'      <actor>{html.escape(clean_xml_text(a))}</actor>\n')
                    f.write('    </credits>\n')
                
                genre = m.get('genre')
                cats = m.get('cats', [])
                if genre: f.write(f'    <category>{html.escape(clean_xml_text(genre))}</category>\n')
                for cat in cats:
                    if cat != genre: f.write(f'    <category>{html.escape(clean_xml_text(cat))}</category>\n')

                img = m.get('img') or p['img']
                if img: f.write(f'    <icon src="{html.escape(img)}" />\n')
                
                if m.get('sn') and m.get('en'):
                    f.write(f'    <episode-num system="onscreen">S{m["sn"]} E{m["en"]}</episode-num>\n')
                
                if m.get('subs'): f.write('    <subtitles type="onscreen" />\n')
                f.write('  </programme>\n')
            f.write('</tv>')

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
