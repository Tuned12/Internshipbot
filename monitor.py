#!/usr/bin/env python3
"""Watches career pages for new internship postings (stage / internship / intern...)
and pings you. Skips alternance. Prefers Paris.
 
  python monitor.py --test        dry run: shows what each site returns, no state, no notifications
  python monitor.py               one check (use with cron / GitHub Actions)
  python monitor.py --loop 120    check forever, every 120s (VPS / Raspberry Pi)
"""
import json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
 
import requests, yaml
from bs4 import BeautifulSoup
 
BASE = Path(__file__).parent
STATE_FILE = BASE / "seen.json"
CONFIG_FILE = BASE / "sites.yaml"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"}
MAX_INDIVIDUAL_PUSHES = 15
 
DEFAULT_INCLUDE = [r"\bstages?\b", r"\bstagiaires?\b", r"\binterns?\b", r"\binternships?\b",
                   r"\bsummer analyst", r"\bspring (week|insight)", r"\btrainee"]
DEFAULT_EXCLUDE = [r"alternan", r"apprenti", r"apprenticeship", r"work[- ]study",
                   r"contrat de professionnalisation", r"contrat pro\b"]
DEFAULT_LOCATIONS = ["paris", "île-de-france", "ile-de-france", "la défense", "la defense",
                     "courbevoie", "puteaux", "nanterre", "issy-les-moulineaux", "boulogne-billancourt",
                     "neuilly", "levallois", "montrouge", "saint-denis"]
# "Stage ou alternance" style titles: you could apply as a stagiaire, so keep them (flagged)
BOTH = re.compile(r"(stages?|stagiaires?|interns?(hips?)?)\s*(/|ou|or|&|et)\s*(alternan|apprenti|apprenticeship|work)"
                  r"|(alternan\w*|apprenti\w*|apprenticeship)\s*(/|ou|or|&|et)\s*(stages?|stagiaires?|intern)", re.I)
 
 
# ---------- fetchers: each returns [{"title","url","location"}] ----------
 
def fetch_greenhouse(site):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{site['token']}/jobs",
                     headers=HEADERS, timeout=25)
    r.raise_for_status()
    return [{"title": j["title"], "url": j["absolute_url"],
             "location": (j.get("location") or {}).get("name", "")} for j in r.json()["jobs"]]
 
 
def fetch_lever(site):
    r = requests.get(f"https://api.lever.co/v0/postings/{site['token']}?mode=json",
                     headers=HEADERS, timeout=25)
    r.raise_for_status()
    return [{"title": j["text"], "url": j["hostedUrl"],
             "location": (j.get("categories") or {}).get("location", "") or ""} for j in r.json()]
 
 
def fetch_smartrecruiters(site):
    out, offset = [], 0
    while offset < 500:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{site['token']}/postings",
                         params={"limit": 100, "offset": offset}, headers=HEADERS, timeout=25)
        r.raise_for_status()
        content = r.json().get("content", [])
        for p in content:
            loc = p.get("location") or {}
            out.append({"title": p["name"],
                        "url": f"https://jobs.smartrecruiters.com/{site['token']}/{p['id']}",
                        "location": f"{loc.get('city', '')} {loc.get('country', '')}".strip()})
        if len(content) < 100:
            break
        offset += 100
    return out
 
 
def fetch_workday(site):
    u = urlparse(site["url"])
    host, tenant = u.netloc, u.netloc.split(".")[0]
    board = [s for s in u.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)][0]
    api = f"https://{host}/wday/cxs/{tenant}/{board}/jobs"
    jobs = {}
    for term in site.get("search", ["stage", "intern"]):
        offset = 0
        while offset < 200:
            r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                         "searchText": term}, headers=HEADERS, timeout=25)
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            for p in posts:
                loc = p.get("locationsText", "")
                if re.match(r"\d+ locations?", loc, re.I):
                    loc = ""
                jobs[p["externalPath"]] = {"title": p["title"], "location": loc,
                                           "url": f"https://{host}/{board}{p['externalPath']}"}
            if len(posts) < 20:
                break
            offset += 20
    return list(jobs.values())
 
 
def fetch_oracle(site):
    """Oracle Recruiting Cloud (J.P. Morgan, Lazard...): read the site's JSON API instead of rendering it."""
    from urllib.parse import parse_qs
    u = urlparse(site["url"])
    q = parse_qs(u.query)
    host = u.netloc
    siteno = re.search(r"/sites/([^/?#]+)", u.path).group(1)
    finder = f"findReqs;siteNumber={siteno},limit=50,sortBy=POSTING_DATES_DESC"
    kw = site.get("keyword") or (q.get("keyword") or [""])[0]
    if kw:
        finder += f",keyword={kw}"
    loc = (q.get("locationId") or [""])[0]
    if loc:
        finder += f",locationId={loc}"
    r = requests.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                     params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = (r.json().get("items") or [{}])[0].get("requisitionList", [])
    return [{"title": j["Title"],
             "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{siteno}/job/{j['Id']}",
             "location": j.get("PrimaryLocation", "") or ""} for j in items]
 
 
def fetch_html(site):
    r = requests.get(site["url"], headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = soup.select(site["selector"]) if site.get("selector") else soup.find_all("a")
    out = []
    for a in links:
        href, title = a.get("href"), a.get_text(" ", strip=True)
        if href and len(title) >= 6 and not href.startswith(("#", "mailto:", "javascript:")):
            out.append({"title": title, "url": urljoin(site["url"], href), "location": ""})
    return out
 
 
_pw = _browser = None
 
 
def _get_browser():
    global _pw, _browser
    if _browser is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("needs a real browser: pip install playwright && playwright install chromium")
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch()
    return _browser
 
 
def close_browser():
    global _pw, _browser
    if _browser:
        _browser.close(); _pw.stop(); _browser = _pw = None
 
 
def fetch_browser(site):
    page = _get_browser().new_page(user_agent=UA, locale="fr-FR")
    try:
        page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(site.get("wait_ms", 2500))
        for _ in range(site.get("scrolls", 2)):  # trigger lazy-loaded job lists
            page.mouse.wheel(0, 4000); page.wait_for_timeout(800)
        pairs = page.eval_on_selector_all(
            site.get("selector", "a"), "els => els.map(e => [(e.innerText||e.textContent||'').trim(), e.href])")
    finally:
        page.close()
    return [{"title": re.sub(r"\s+", " ", t), "url": h, "location": ""}
            for t, h in pairs if h.startswith("http") and len(t) >= 6]
 
 
def fetch_auto(site):
    """Plain HTTP first (fast); if the page is an empty JS shell, retry with a real browser."""
    try:
        jobs = fetch_html(site)
    except Exception:
        jobs = []
    if len(jobs) >= 8:
        return jobs
    return fetch_browser(site)
 
 
FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "smartrecruiters": fetch_smartrecruiters,
            "workday": fetch_workday, "oracle": fetch_oracle, "html": fetch_html, "browser": fetch_browser, "auto": fetch_auto}
SCRAPED_TYPES = {"html", "browser", "auto"}
 
 
# ---------- filtering ----------
 
def compile_cfg(config):
    rx = lambda pats: [re.compile(p, re.I) for p in pats]
    return {"include": rx(config.get("include", DEFAULT_INCLUDE)),
            "exclude": rx(config.get("exclude", DEFAULT_EXCLUDE)),
            "locations": [l.lower() for l in config.get("locations", DEFAULT_LOCATIONS)]}
 
 
def judge(job, site, cfg):
    """Returns None (ignore) or a flag string ('' = clean match)."""
    title = job["title"]
    if site.get("url_contains") and site["url_contains"] not in job["url"]:
        return None                      # only real job-card links, not menus/footers
    if not site.get("all_match") and not any(r.search(title) for r in cfg["include"]):
        return None                      # (all_match = the page URL is already filtered to internships)
    flag = ""
    if any(r.search(title) for r in cfg["exclude"]):
        if not BOTH.search(title):
            return None
        flag = " (also mentions alternance)"
    if site.get("any_location"):
        return flag
    loc = (job.get("location") or "").lower()
    if loc:
        return flag if any(l in loc for l in cfg["locations"]) else None
    need = site.get("require_text")
    if need and not any(n.lower() in title.lower() for n in need):
        return None
    return flag
 
 
# ---------- notifications ----------
 
def push(title, body, url=None):
    print(f"[notify] {title} | {body} | {url or ''}")
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        h = {"Title": title.encode("utf-8"), "Tags": "briefcase"}
        text = body
        if url:
            h["Click"] = url          # tapping the notification opens the job directly
            text = f"{body}\n{url}".strip()   # and the link is also written in the message
        requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), headers=h, timeout=15)
    hook = os.getenv("DISCORD_WEBHOOK")
    if hook:
        parts = [f"**{title}**", body, url or ""]
        requests.post(hook, json={"content": "\n".join(x for x in parts if x)[:1900]}, timeout=15)
 
 
def notify_new(new_items):
    if len(new_items) <= MAX_INDIVIDUAL_PUSHES:
        for site, title, url, flag in new_items:
            company = re.sub(r"\s*\(.*?\)", "", site).strip()   # "CDC (Wizbii)" -> "CDC"
            push(f"New stage {company}, {title}{flag}", "", url)
    else:  # avoid blowing up your phone: one digest per site
        by_site = {}
        for site, title, url, flag in new_items:
            by_site.setdefault(site, []).append(f"- {title}{flag}\n  {url}")
        for site, lines in by_site.items():
            push(f"{len(lines)} new stages: {site}", "\n".join(lines[:10]))
 
 
# ---------- main ----------
 
def evaluate(jobs, site, cfg):
    matches, flags = {}, {}
    for j in jobs:
        f = judge(j, site, cfg)
        if f is not None:
            matches[j["url"]] = j["title"]
            flags[j["url"]] = f
    return matches, flags
 
 
def get_jobs(site, cfg):
    stype = site.get("type", "auto")
    if stype == "oracle":
        try:
            jobs = fetch_oracle(site)
            if jobs:
                return jobs
            print(f"   [note] {site['name']}: Oracle API returned 0 jobs, trying the browser", file=sys.stderr)
        except Exception as e:
            print(f"   [note] {site['name']}: Oracle API failed ({e}), trying the browser", file=sys.stderr)
        return fetch_browser(site)
    if stype != "auto":
        return FETCHERS[stype](site)
    # auto: fast plain request first; if it shows no internships (JS-built page), retry in a real browser
    jobs, err = [], None
    try:
        jobs = fetch_html(site)
    except Exception as e:
        err = e
    if not evaluate(jobs, site, cfg)[0]:
        try:
            jobs = list({j["url"]: j for j in jobs + fetch_browser(site)}.values())
        except Exception as e:
            err = err or e
    if not jobs and err:
        raise err
    return jobs
 
 
def run(test=False):
    config = yaml.safe_load(CONFIG_FILE.read_text())
    cfg = compile_cfg(config)
    state = {} if test or not STATE_FILE.exists() else json.loads(STATE_FILE.read_text())
    fails = state.get("_fail", {})
    migrating = "_urls" not in state          # first run of this version: stay quiet, just memorize
    urls = state.get("_urls", {})
    new_items = []
    checked = 0
 
    for site in config["sites"]:
        name = site["name"]
        try:
            jobs = get_jobs(site, cfg)
            if site.get("type", "auto") in SCRAPED_TYPES and len(jobs) < 3:
                raise RuntimeError(f"only {len(jobs)} links found (page blocked or needs a better URL)")
        except Exception as e:
            fails[name] = fails.get(name, 0) + 1
            print(f"[error] {name}: {e}", file=sys.stderr)
            if not test and fails[name] == 4 and not site.get("quiet"):  # ~4 failed checks in a row -> tell the user once
                push("Bot: site not working", f"{name} keeps failing: {e}")
            continue
        fails.pop(name, None)
        checked += 1
        matches, flags = evaluate(jobs, site, cfg)
 
        if test:
            print(f"\n== {name} [{site.get('type', 'auto')}]: {len(jobs)} links scanned, {len(matches)} internships")
            for u, t in list(matches.items())[:15]:
                print(f"   - {t}{flags[u]}\n     {u}")
            continue
 
        # new site, or its URL changed -> memorize silently instead of alerting about everything
        reseed = migrating or name not in state or urls.get(name) != site["url"]
        seen = set() if reseed else set(state.get(name, []))
        if reseed:
            print(f"[init] {name}: scanned {len(jobs)} links, {len(matches)} internships already open (no alert)")
            if not matches:  # help debugging: show what kind of links the page gave us
                for j in sorted(jobs, key=lambda j: -len(j["title"]))[:6]:
                    print(f"   [sample] {j['title'][:70]} | {j['url'][:110]}")
        else:
            fresh = [(name, t, u, flags[u]) for u, t in matches.items() if u not in seen]
            new_items += fresh
            print(f"[ok] {name}: scanned {len(jobs)} links, {len(matches)} internships, {len(fresh)} new")
        urls[name] = site["url"]
        state[name] = sorted(seen | set(matches))
 
    close_browser()
    if not test:
        active = {x["name"] for x in config["sites"]}
        for k in [k for k in state if not k.startswith("_") and k not in active]:
            del state[k]                       # site was removed from sites.yaml -> forget it
        fails = {k: v for k, v in fails.items() if k in active}
        urls = {k: v for k, v in urls.items() if k in active}
        state["_fail"] = fails
        state["_urls"] = urls
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Paris"))
        today = now.strftime("%Y-%m-%d")
        if now.hour >= int(os.getenv("HB_HOUR", "8")) and state.get("_hb") != today:
            bad = ", ".join(sorted(fails)) or "none"
            push("Bot is alive", f"{checked}/{len(active)} sites checked OK this run. Failing: {bad}")
            state["_hb"] = today
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
        if new_items:
            notify_new(new_items)
 
 
if __name__ == "__main__":
    if "--loop" in sys.argv:
        wait = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            try:
                run()
            except Exception as e:
                print(f"[fatal] {e}", file=sys.stderr)
            time.sleep(wait)
    else:
        run(test="--test" in sys.argv)
 
