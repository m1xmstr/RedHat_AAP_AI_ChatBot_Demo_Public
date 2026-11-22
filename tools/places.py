"""Extract named venues from first-party markup and build keyless Maps links."""
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

BOOK = re.compile(r"\b(?:book\s?(?:shop|store)s?)\b", re.I)
SHOP = re.compile(r"\b(?:stores?|shops?|markets?)\b", re.I)
FOOD = re.compile(r"\b(?:caf[eé]|coffeehouse|coffee shop|deli|grill[e]?|restaurant|dining hall|bakery|bistro|pizzeria|steakhouse|eatery)\b", re.I)
SCHOOL = re.compile(r"\b(?:university|college|academy|school|campus)\b", re.I)
STREET = re.compile(r"\b\d{1,6}\s+[\w .’'&-]{1,55}?\b(?:road|rd|street|st|avenue|ave|lane|ln|drive|dr|court|ct|boulevard|blvd|way|plaza)\b", re.I)
GENERIC = re.compile(r"^(?:(?:all|our|campus|student|on campus|on-campus) )?(?:locations?|venues?|dining|dining halls?|restaurants?|bookstores?|bookshops?|stores?|hours|menus?|campus|map|directions|food|coffee shops?)$", re.I)
ACTION = re.compile(r"\b(?:learn more|find a|find us|contact us|order online|get directions|skip to|back to top|open menu|view menu|mobile ordering|onecard login|sign in|log in|see all|view all|what.s open|hours of operation|meal plans?|read more|opening hours)\b", re.I)
TYPES = {
    "dining": {"restaurant", "foodestablishment", "fastfoodrestaurant", "bakery", "cafeorcoffeeshop", "barorpub", "icecreamshop", "brewery"},
    "bookstore": {"bookstore"}, "store": {"store", "grocerystore", "conveniencestore", "clothingstore", "departmentstore", "electronicsstore"},
    "campus": {"collegeoruniversity", "school", "highschool", "elementaryschool", "middleschool"},
}


def clean(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def valid_name(name):
    return 2 <= len(name) <= 90 and not GENERIC.fullmatch(name) and not ACTION.search(name) and not re.search(r"[?<>\n]|https?://", name, re.I)


class PlaceMarkup(HTMLParser):
    """Keep structure which the text crawler legitimately removes, including visible map anchor text."""
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.active = []
        self.suppressed = []
        self.headings = []
        self.links = []
        self.addresses = []
        self.sections = []
        self.structured = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        void = tag in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr")
        if self.suppressed or tag in ("template", "noscript", "style") or "hidden" in attrs or attrs.get("aria-hidden") == "true":
            if not void:
                self.suppressed.append(tag)
            return
        if tag == "br":
            self.handle_data(" ")
        if (tag in ("a", "address") or re.fullmatch(r"h[1-6]", tag) or
                tag == "script" and (attrs.get("type") or "").split(";")[0].strip().lower() == "application/ld+json"):
            self.active.append((tag, attrs, []))

    def handle_data(self, data):
        if self.suppressed:
            return
        for _, _, parts in self.active:
            parts.append(data)

    def handle_endtag(self, tag):
        if self.suppressed:
            if tag in self.suppressed:
                index = len(self.suppressed) - 1 - self.suppressed[::-1].index(tag)
                del self.suppressed[index:]
            return
        for index in range(len(self.active)-1, -1, -1):
            if self.active[index][0] == tag:
                _, attrs, parts = self.active.pop(index)
                break
        else:
            return
        if tag == "script":
            raw = "".join(parts)
            if len(raw) <= 250_000:
                try:
                    self.structured.append(json.loads(raw))
                except (ValueError, RecursionError):
                    pass
            return
        text = clean(" ".join(parts))
        if not text or len(text) > 400:
            return
        if tag == "a":
            target_value = (attrs.get("href") or "").strip()
            if not target_value or target_value.startswith("#"):
                return
            try:
                href = urljoin(self.url, target_value)
                target = urlsplit(href)
            except ValueError:
                return
            if target.scheme.lower() in ("http", "https") and target.hostname and len(self.links) < 2000:
                self.links.append({"text": text, "href": href})
        elif tag == "address":
            self.addresses.append(text)
            self.sections.append(("address", text))
        else:
            self.headings.append([int(tag[1]), text])
            self.sections.append((tag, text))


def extract_place_markup(html, base_url=""):
    parser = PlaceMarkup(base_url)
    parser.feed(html)
    parser.close()
    return {"structured": parser.structured, "headings": parser.headings, "links": parser.links, "addresses": parser.addresses, "sections": parser.sections}


def schema_entities(data):
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            if item.get("@type"):
                yield item
            pending.extend(x for x in item.values() if isinstance(x, (list, dict)))


def schema_address(entity):
    if isinstance(entity, str):
        return clean(entity)
    if isinstance(entity, dict):
        return ", ".join(filter(None, (clean(entity.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode"))))
    return ""


def key(name):
    return re.sub(r"[^\w]+", " ", name.casefold()).strip()


def category(name, context=""):
    if BOOK.search(name) or re.search(r"\bstudent stores?\b", name, re.I) and re.search(r"course materials|textbooks?|bookstore|bookshop", context, re.I):
        return "bookstore"
    if FOOD.search(name):
        return "dining"
    if SHOP.search(name):
        return "store"
    return ""


def official(host, root):
    return bool(host and root and (host == root or host.endswith("." + root)))


def page_places(page, root):
    title = clean(page.get("title"))
    text = page.get("text", "")
    data = page.get("place_data") or {}
    source = urlsplit(page["url"])
    headings = data.get("headings", [])
    dining_site = bool(re.search(r"dining|restaurants?", (source.hostname or "") + source.path, re.I))
    dining_detail = dining_site and re.search(r"/(?:locations?|restaurants?|venues?)/[^/]+/?$", source.path, re.I)
    primary_title = re.split(r"\s+[|–—-]\s+", title)[0].strip()
    primary = next((value for level, value in headings if level == 1 and key(value) == key(primary_title)
                    and valid_name(value)), "")
    for item in schema_entities(data.get("structured", [])):
        raw = item["@type"] if isinstance(item["@type"], list) else [item["@type"]]
        kinds = {str(t).rsplit("/", 1)[-1].lower() for t in raw}
        kind = next((k for k, types in TYPES.items() if types & kinds), "")
        name, address = clean(item.get("name")), schema_address(item.get("address"))
        if kind and valid_name(name) and (not dining_detail or primary and key(name) == key(primary)):
            yield name, kind, address, (f"{name} — {address}" if address else name)

    if dining_detail:
        # Venue detail pages contain food items, booking links and site navigation.
        # Only the named subject confirmed by both the title and H1 is a venue.
        if primary:
            yield primary, "dining", "", f"{title} lists {primary}."
        return

    links = data.get("links", [])
    addresses = data.get("addresses", [])
    # On a building page the address belongs to the building, not to every link in the footer.
    mapped_addresses = [l["text"] for l in links if STREET.search(l["text"]) and
                        urlsplit(l["href"]).hostname in ("www.google.com", "maps.google.com", "maps.app.goo.gl")]
    official_dining = {}
    for link in links:
        dest = urlsplit(link["href"])
        if dest.hostname == source.hostname and dest.path.rstrip("/") == source.path.rstrip("/"):
            continue
        if official(dest.hostname, root) and re.search(r"dining|restaurant|eatery", (dest.hostname or "") + dest.path, re.I) and re.search(r"/(?:locations?|restaurants?|venues?|dining)/[^/]+/?$",dest.path,re.I):
            official_dining[key(link["text"])] = link["text"]

    building = ""
    for level, heading in headings:
        if level <= 2:
            m = re.fullmatch(r"(.+?)\s+Building Hours", heading, re.I)
            building = clean(m[1]) if m else ""
        name = re.sub(r"\s+(?:Hours\s*(?:&|and)\s*Location|Hours|Menu)$", "", heading, flags=re.I).strip()
        if key(name) in official_dining and valid_name(name):
            address = ""
            evidence = f"{heading} is listed by {title}."
            if building:
                address = building
                evidence = f"{name} is listed under {building} Building Hours."
                if len(set(mapped_addresses)) == 1:
                    address += ", " + mapped_addresses[0]
                    evidence += " The building address listed on the page is " + mapped_addresses[0] + "."
            yield name, "dining", address, evidence

    directory = dining_site and bool(re.fullmatch(r"/(?:[^/]+/)*(?:locations?|restaurants?|venues?|dining)/?", source.path, re.I))
    if directory:
        for name in official_dining.values():
            if valid_name(name) and not re.search(r"\bmenu\b|locations?|dining services",name,re.I):
                yield name, "dining", "", f"{title} lists {name}."

    # Pair names and addresses within a named venue section of an official
    # directory. A later unrelated heading closes the section.
    directory_title = " ".join([title, *(value for level, value in headings if level == 1)])
    directory_kind = ""
    if re.search(r"dining locations|where to eat|restaurant directory|campus dining", directory_title, re.I):
        directory_kind = "dining"
    elif re.search(r"store locations|store directory|campus stores|bookstore locations", directory_title, re.I):
        directory_kind = "store"
    current = ""
    for tag, value in data.get("sections", []):
        if tag in ("h1", "h2", "h3", "h4"):
            candidate = re.sub(r"\s+(?:hours|location)$", "", value, flags=re.I).strip()
            current = candidate if valid_name(candidate) and len(candidate.split()) <= 9 else ""
        elif tag == "address" and current and STREET.search(value):
            kind = category(current) or directory_kind
            if kind and (directory_kind or key(current) == key(title.split("|")[0].strip())):
                address = re.sub(r"^address\s*:\s*", "", value, flags=re.I)
                yield current, kind, address, f"{current} — {address}"
            current = ""

    # A publisher can explicitly locate a store by naming its building before a street
    # address, even when the page is a department (e.g. a pharmacy within that store).
    for raw in addresses:
        raw = re.sub(r"^address\s*:\s*", "", raw, flags=re.I)
        name, sep, street = raw.partition(",")
        floor = re.search(r"\b(?:floor|suite|room|level)\b", name, re.I)
        if sep and not floor and STREET.search(street) and (SHOP.search(name) or BOOK.search(name)) and valid_name(name):
            yield name.strip(), category(name), clean(street), raw

    lines = [clean(l) for l in text.splitlines() if l.strip()]
    pdf = source.path.lower().endswith(".pdf")
    if pdf:
        for i, line in enumerate(lines):
            combined = re.split(r"\s+AND\s+", line) if line.isupper() else []
            if len(combined) == 2 and all(category(part, text) and valid_name(part) for part in combined):
                nearby = " ".join(lines[i+1:i+14])
                for part in combined:
                    occurrence = re.search(re.escape(part), nearby, re.I)
                    name = occurrence[0] if occurrence else part.title()
                    yield name, category(name, text), "", f"{title} lists {name}."
                continue
            kind = category(line, text)
            if not kind or not valid_name(line) or len(line.split()) > 8 or re.search(r"[.!?:|]",line):
                continue
            context = " ".join(lines[i+1:i+6])
            name = line
            if line.isupper():
                # Preserve the publisher's own display case when the paragraph names it.
                match = re.search(re.escape(line), context, re.I)
                name = match[0] if match else line.title()
            lead = re.match(r"(?:The )?" + re.escape(name) + r"\s+(?:is\s+(?:conveniently\s+)?located|is\s+on|can be found)\s+([^.!?]{4,140})",context,re.I)
            locator = clean(lead[1]) if lead else ""
            if not locator and i+1 < len(lines):
                loc = lines[i+1].split("|")[0].strip()
                if re.fullmatch(r"[\w ’'-]+ (?:Building|Hall|Center|Centre)",loc,re.I):
                    locator = loc.title() if loc.isupper() else loc
            if locator or re.match(r"(?:The )?" + re.escape(name) + r"\b", context, re.I):
                yield name, kind, locator, f"{name} — {locator}" if locator else f"{title} lists {name}."

    # A homepage is a campus map destination only when the school itself appears in its title.
    if source.path in ("", "/") and (source.hostname or "").removeprefix("www.") == root:
        names = [s.strip() for s in re.split(r"\s+[|–—]\s+|\s+-\s+", title)]
        school = next((n for n in names if SCHOOL.search(n) and valid_name(n)), "")
        if school:
            yield school, "campus", "", title



def customer_location(records, customer_name, website):
    """Resolve the customer and preserve the official record behind its unique address."""
    site = urlsplit(website if "://" in website else "https://" + website)
    host = (site.hostname or "").removeprefix("www.")

    def identity(name):
        return re.sub(r"^the ", "", key(name))

    requested = identity(customer_name)
    candidates = []
    for record in records:
        page = urlsplit(record["source_url"])
        if not official(page.hostname, host):
            continue
        named = identity(record["name"])
        exact = named == requested
        # A named campus can disambiguate a university system's public name.
        # Exclude departments and check for competing campus names below.
        qualified = (record["category"] == "campus" and named.startswith(requested + " at ")
                     and not re.search(r"\b(?:college|school|institute|department|faculty|clinic|center|centre)\b",
                                       named[len(requested):]))
        if exact or qualified:
            home = (page.hostname or "").removeprefix("www.") == host and page.path.rstrip("/") in ("", site.path.rstrip("/"))
            candidates.append((home, exact, named, record))
    if not candidates:
        return customer_name, None
    # Prefer the identity on the customer's homepage. If equally credible names
    # or branches disagree, a Maps name search avoids picking an arbitrary one.
    rank = max((home, exact) for home, exact, _, _ in candidates)
    preferred = [(name, r) for home, exact, name, r in candidates if (home, exact) == rank]
    names = {name for name, _ in preferred}
    if len(names) != 1:
        return customer_name, None
    name, chosen = preferred[0]
    addresses = {r["address"]: r for _, _, identity_name, r in candidates if identity_name == name and r["address"]}
    return chosen["name"], next(iter(addresses.values())) if len(addresses) == 1 else None


def customer_destination(records, customer_name, website):
    name, record = customer_location(records, customer_name, website)
    return name, record["address"] if record else ""


def customer_map_url(records, customer_name, website):
    name, address = customer_destination(records, customer_name, website)
    return "https://www.google.com/maps/search/?" + urlencode({"api":1, "query":", ".join(filter(None,(name,address)))})


def extract_places(pages, customer_name, website):
    root = (urlsplit(website if "://" in website else "https://" + website).hostname or "").removeprefix("www.")
    rows = {}
    for page in pages:
        url = page.get("url", "")
        parsed = urlsplit(url)
        if parsed.scheme not in ("https","http") or not official(parsed.hostname, root):
            continue
        for name, kind, address, evidence in page_places(page, root):
            if not kind or not valid_name(name):
                continue
            row = {"name":name, "category":kind, "address":address, "source_url":url,
                   "source_title":clean(page.get("title")) or url, "evidence":clean(evidence)}
            identity = key(name)
            old = rows.get(identity)
            if old and old["address"] and address and key(old["address"]) != key(address):
                # Preserve distinct branches when each has a verified street address.
                if STREET.search(old["address"]) and STREET.search(address):
                    identity += " " + key(address)
                    old = rows.get(identity)
            if not old or (bool(STREET.search(address)), bool(address), len(evidence)) > (bool(STREET.search(old["address"])),bool(old["address"]),len(old["evidence"])):
                rows[identity] = row

    records = list(rows.values())
    # Link a named store building to its unique handbook entry only when that entry
    # explicitly identifies the same building. Keep unrelated branches separate.
    for building in list(records):
        match = re.fullmatch(r"(.+?)\s+(Student Stores?)", building["name"], re.I)
        if not match or not STREET.search(building["address"]):
            continue
        peers = [r for r in records if r is not building and r["name"].casefold().endswith(match[2].casefold()) and
                 re.search(r"\b" + re.escape(match[1]) + r"\s+Building\b", r["address"],re.I)]
        if len(peers) == 1:
            building["category"] = peers[0]["category"]
            records.remove(peers[0])
    campus, _ = customer_destination(records, customer_name, website)
    for record in records:
        query = ", ".join(dict.fromkeys(filter(None,(record["name"], record["address"], campus))))
        record["maps_url"] = "https://www.google.com/maps/search/?" + urlencode({"api":1,"query":query})
        record["directions_url"] = "https://www.google.com/maps/dir/?" + urlencode({"api":1,"destination":query})
    return records


def place_pages(records):
    pages = defaultdict(list)
    for r in records:
        pages[(r["source_url"],r["source_title"])].append(
            f"{r['category'].capitalize()} location: {r['name']}. {r['evidence']}")
    return [{"url":url, "title":title, "text":"\n".join(lines)} for (url,title),lines in pages.items()]
