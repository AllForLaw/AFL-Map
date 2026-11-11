"""Rebuild the map data embedded in index.html from the Google Sheet.

Usage: SHEET_CSV_URL=... python update_map.py
Only aggregate counts (no names / phones / emails) are written to the page.
New cities are geocoded with OpenStreetMap Nominatim and cached in geocache.json.
"""
import collections, csv, io, json, os, re, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "index.html")
CACHE = os.path.join(HERE, "geocache.json")
UA = {"User-Agent": "AFL-Map-updater (github.com/AllForLaw/AFL-Map)"}
MAP_RE = re.compile(r'(<script id="mapdata" type="application/json">)(.*?)(</script>)', re.S)

# Spreadsheet spellings -> names used by the map's shapes. Add to these when the log says "skipped".
STATE_ALIAS = {
    "new delhi": "NCT of Delhi", "delhi": "NCT of Delhi", "nct of delhi": "NCT of Delhi",
    "arunachal pradesh": "Arunanchal Pradesh", "pondicherry": "Puducherry", "orissa": "Odisha",
    "jammu and kashmir": "Jammu & Kashmir", "j&k": "Jammu & Kashmir", "ladakh": "Jammu & Kashmir",
    "andaman and nicobar islands": "Andaman & Nicobar Island", "uttaranchal": "Uttarakhand",
}
COUNTRY_ALIAS = {
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "london": "United Kingdom", "uk": "United Kingdom", "northern ireland": "United Kingdom",
    "usa": "United States of America", "united states": "United States of America", "us": "United States of America",
    "tanzania": "United Republic of Tanzania", "serbia": "Republic of Serbia", "uae": "United Arab Emirates",
    "côte d'ivoire": "Ivory Coast", "eswatini": "Swaziland", "north macedonia": "Macedonia",
}
CITY_ALIAS = {"bangalore": "Bengaluru", "coochbehar": "Cooch Behar", "bahwalpur": "Bahawalpur",
              "gurgaon": "Gurugram", "bombay": "Mumbai", "calcutta": "Kolkata", "madras": "Chennai"}
CITY_STATE = {"chandigarh": "Chandigarh"}  # city whose state the sheet often gets wrong
BLANK = {"", "na", "n/a", "not available", "-", ".", "nil", "none"}


def clean(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return "" if s.lower() in BLANK else s


def nominatim(q):
    time.sleep(1.1)  # Nominatim policy: max 1 request/second
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {**q, "format": "json", "limit": 1, "addressdetails": 1, "accept-language": "en"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            res = json.load(r)
    except Exception as e:
        print("  geocode error:", e)
        return None
    return res[0] if res else None


def bbox_centre(geom):
    # ponytail: bbox centre, not true centroid; fine for a fallback dot
    pts = re.findall(r"\[(-?[\d.]+),\s*(-?[\d.]+)\]", json.dumps(geom))
    xs, ys = [float(x) for x, _ in pts], [float(y) for _, y in pts]
    return round((min(ys) + max(ys)) / 2, 4), round((min(xs) + max(xs)) / 2, 4)


def main():
    html = open(HTML, encoding="utf8").read()
    data = json.loads(MAP_RE.search(html).group(2))
    states = {f["properties"]["ST_NM"]: f for f in data["indiaStates"]["features"]}
    countries = {f["properties"]["name"]: f for f in data["world"]["features"]}
    state_l = {k.lower(): k for k in states}
    country_l = {k.lower(): k for k in countries}

    cache = json.load(open(CACHE, encoding="utf8")) if os.path.exists(CACHE) else {}
    for c in data["cities"]:  # seed cache with coordinates already on the map
        cache.setdefault(f"{c['city']}|{c['admin'] or ''}|{c['country']}".lower(), [c["lat"], c["lon"]])
    city_home = {}  # city name -> (state, country) learned from cache, for rows missing a state
    for k in cache:
        city, st, co = k.split("|")
        city_home.setdefault(city, (state_l.get(st), country_l.get(co)))

    def region(name):
        """Return (state, country) for a State-column value, or None."""
        n = name.lower()
        if n in STATE_ALIAS: return STATE_ALIAS[n], "India"
        if n in state_l: return state_l[n], "India"
        if n in COUNTRY_ALIAS: return None, COUNTRY_ALIAS[n]
        if n in country_l: return None, country_l[n]
        return None

    url = os.environ.get("SHEET_CSV_URL") or sys.exit("SHEET_CSV_URL not set")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        rows = list(csv.reader(io.StringIO(r.read().decode("utf-8-sig"))))
    head = [h.lower() for h in rows[0]]
    col = lambda word: next(i for i, h in enumerate(head) if word in h)
    iu, ip, ic, ist, ie = col("university"), col("programme"), col("city"), col("state"), col("email")

    groups = collections.defaultdict(list)  # (city, state|None, country) -> rows
    skipped = collections.Counter()
    for r in rows[1:]:
        r += [""] * (len(head) - len(r))
        city, st = clean(r[ic]), clean(r[ist])
        city = CITY_ALIAS.get(city.lower(), city.title() if city.isupper() or city.islower() else city)
        reg = region(st) if st else None
        if not reg and city:  # state blank/unknown: is the city itself a state/country, or a known city?
            reg = region(city) or city_home.get(city.lower())
        if not reg and city and not st and city.lower() not in city_home:
            g = nominatim({"q": city})  # look the city up to find its state/country
            a = (g or {}).get("address", {})
            co = a.get("country", "")
            reg = region(a.get("state", "")) if co == "India" else region(co)
            city_home[city.lower()] = reg
        if reg and not reg[1]:
            reg = None
        if not reg:
            skipped[f"{city or '?'} / {st or '?'}"] += 1
            continue
        sname, country = reg
        if country == "India" and city.lower() in CITY_STATE:
            sname = CITY_STATE[city.lower()]
        if not city:
            city = sname or country  # state/country-level dot
        groups[(city, sname, country)].append(r)

    cities = []
    for (city, sname, country), rs in groups.items():
        key = f"{city}|{sname or ''}|{country}".lower()
        if key not in cache:
            q = {"city": city, "country": country}
            if sname: q["state"] = sname
            g = None if city in (sname, country) else nominatim(q)
            if not g and sname:  # sheet's state may be wrong/odd spelling: retry with country only
                g = nominatim({"city": city, "country": country})
            if g:
                cache[key] = [round(float(g["lat"]), 4), round(float(g["lon"]), 4)]
            else:  # fall back to the middle of the state / country
                cache[key] = list(bbox_centre((states[sname] if sname else countries[country])["geometry"]))
            print("  geocoded", key, cache[key])
        lat, lon = cache[key]
        cities.append({"city": city, "admin": sname, "country": country, "lat": lat, "lon": lon, "total": len(rs)})
    cities.sort(key=lambda c: -c["total"])

    by_state, by_country = collections.defaultdict(list), collections.defaultdict(list)
    for (city, sname, country), rs in groups.items():
        by_country[country].append((city, rs))
        if sname: by_state[sname].append((city, rs))

    for name, f in states.items():
        f["properties"]["total"] = sum(len(rs) for _, rs in by_state.get(name, []))
        f["properties"]["cityCount"] = len(by_state.get(name, []))
    for name, f in countries.items():
        f["properties"]["total"] = sum(len(rs) for _, rs in by_country.get(name, []))

    def stats(parts):
        rs = [r for _, p in parts for r in p]
        unis = collections.Counter(clean(r[iu]) for r in rs if clean(r[iu]))
        progs = collections.Counter(clean(r[ip]) for r in rs if clean(r[ip]))
        top = lambda c: [{"name": n, "count": k} for n, k in c.most_common(3)]
        return {"contacts": len(rs), "cities": len(parts), "universities": len(unis),
                "emailPct": round(100 * sum("@" in r[ie] for r in rs) / len(rs)),
                "topProgrammes": top(progs), "topUniversities": top(unis)}

    analytics = {s: stats(p) for s, p in by_state.items()}
    analytics.update({c: stats(p) for c, p in by_country.items() if c != "India"})

    data["cities"], data["analytics"] = cities, analytics
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    open(HTML, "w", encoding="utf8", newline="").write(MAP_RE.sub(lambda m: m.group(1) + blob + m.group(3), html, 1))
    json.dump(dict(sorted(cache.items())), open(CACHE, "w", encoding="utf8"), ensure_ascii=False, indent=0)

    print(f"{sum(c['total'] for c in cities)} rows mapped across {len(cities)} places; {sum(skipped.values())} skipped")
    for k, v in skipped.most_common():
        print(f"  skipped {v}x  {k}")


if __name__ == "__main__":
    main()
