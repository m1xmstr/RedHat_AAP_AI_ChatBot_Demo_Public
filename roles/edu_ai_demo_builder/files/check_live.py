"""Check the public customer route, its downloadable corpus, and one cited answer."""
import argparse
import calendar
from collections import Counter
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def request(url, data=None, build=None):
    body = json.dumps(data).encode() if data is not None else None
    context = ssl.create_default_context()
    ca_file = os.environ.get("DEMO_ROUTE_CA_FILE") or os.environ.get("K8S_AUTH_SSL_CA_CERT")
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    headers = {"Content-Type": "application/json"}
    if build:
        headers["X-Demo-Expected-Build"] = build
    req = urllib.request.Request(url, body, headers=headers)
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(req, timeout=150, context=context) as reply:
                if not build or reply.headers.get("X-Demo-Build") == build:
                    return reply.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 409 or not build:
                raise
            exc.close()
        if time.monotonic() >= deadline:
            raise RuntimeError("The route is still serving a different customer build")
        time.sleep(2)


AI_PROVIDERS = {"openshift_ai", "rhel_ai", "rhel_vm"}
UNC_QUESTIONS = ("When is enrollment", "How many bookstores are on campus?", "Where can I eat on campus?")


def words(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold().replace("’", "").replace("'", "")))


def inventory_check(profile, manifest, chunks):
    counts = Counter()
    for chunk in chunks:
        url = urlsplit(chunk["url"])
        if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
                or not all(str(chunk.get(k, "")).strip() for k in ("id", "title", "text"))):
            raise ValueError("The downloaded RAG file contains an incomplete or invalid source")
        counts[chunk["url"]] += 1
    rows = manifest["sources"]
    if (not chunks or len({row["id"] for row in chunks}) != len(chunks)
            or manifest["customer"] != profile["customer_name"]
            or not len(chunks) == manifest["passage_count"] == profile["chunk_count"]
            or not len(counts) == len(rows) == manifest["source_count"] == profile["page_count"]
            or {row["url"]: row["passages"] for row in rows} != dict(counts)):
        raise ValueError("Source inventory, profile and downloadable RAG passage counts disagree")
    return set(counts)


def cited_answer(url, question, provider, sources, build=None):
    result = json.loads(request(url + "/api/chat", {"question": question}, build))
    cited = {s["url"] for s in result.get("sources", [])}
    expected = provider if provider in AI_PROVIDERS else "evidence"
    if (result.get("confidence") != "evidence_found" or not cited or not cited <= sources
            or not result.get("answer", "").strip() or result.get("provider") != expected):
        observed = result.get("provider") or "unknown"
        confidence = result.get("confidence") or "unknown"
        raise ValueError(f"Missing a grounded {expected} answer for {question!r}. "
                         f"Observed provider={observed}, confidence={confidence}, "
                         f"citations={len(cited)}, citations outside this build={len(cited - sources)}")
    return result["answer"].strip(), cited


def dates(text):
    """Normalize dates from model prose and registrar events to comparable tuples."""
    result = {(int(month), int(day), int(year)) for year, month, day in
              re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)}
    result.update((int(month), int(day), int(year) if year else None) for month, day, year in
                  re.findall(r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/(20\d{2}))?\b", text))
    for i in range(1, 13):
        pat = rf"\b(?:{calendar.month_name[i]}|{calendar.month_abbr[i]})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b"
        for day, year in re.findall(pat, text, re.I):
            result.add((i, int(day), int(year) if year else None))
    return result


def enrollment_supported(answer, chunks):
    text = words(answer)
    process = re.compile(r"connectcarolina|enrollment appointment|registration appointment|registration time ticket")
    def related(line):
        return re.search(r"enroll|registration|register", line, re.I)
    events = [line for chunk in chunks for line in chunk["text"].splitlines() if related(line)]
    if not events or not related(answer):
        return False
    given = dates(answer)
    if given:
        valid = set().union(*(dates(line) for line in events if not re.search(
            r"withdrawal|classes begin|start of classes|admissions? deadline|enrollment deposit", line, re.I)
            and (not re.search(r"last day|registration ends|enrollment ends|registration close|deadline", line, re.I)
                 or re.search(r"last day|closes?|ends?|deadline", answer, re.I))))
        if not all(any(d[:2] == stated[:2] and (stated[2] is None or d[2] is None or d[2] == stated[2])
                       for d in valid) for stated in given):
            return False
        # A registration date belongs to a term/group. Require the qualifier itself to appear in a cited source.
        terms = set(re.findall(r"\b(?:spring|fall|summer|autumn|winter)\s+20\d{2}\b", text))
        source = words(" ".join(c["title"]+" "+c["text"] for c in chunks))
        return bool(terms) and all(term in source for term in terms)
    return bool(process.search(text) and process.search(words(" ".join(events)))
                and re.search(r"term|semester|appointment|student group", text))


def matched_places(result, cited, chunks, places, category):
    answer = " " + words(result) + " "
    source_text = {url: " " + words(" ".join(c["text"] for c in chunks if c["url"] == url)) + " " for url in cited}
    matches = []
    for place in places:
        if place.get("category") != category or place.get("source_url") not in cited:
            continue
        name = words(place["name"])
        aliases = {name, re.sub(r"^(?:unc |the )", "", name)}
        if any(" "+alias+" " in answer and " "+alias+" " in source_text[place["source_url"]] for alias in aliases if alias):
            matches.append(place)
    return matches


COUNTS = r"zero|one|two|three|four|five|six|seven|eight|nine|ten|\d+"


def bookstore_supported(answer, cited, chunks, places):
    if not matched_places(answer, cited, chunks, places, "bookstore"):
        return False
    text = words(answer)
    # “One example” or “at least one” doesn't assert a campus-wide total.
    count_text = re.sub(rf"\b(?:at least|one of|can (?:verify|confirm)|identif(?:y|ied)|lists?|mentions?) (?:{COUNTS})\b", "", text)
    totals = re.findall(rf"\b({COUNTS}) (?:(?:campus|main|official|on campus|university operated|college) )?(?:bookstores?|bookshops?)\b", count_text)
    totals += re.findall(rf"\b(?:there (?:are|is)|the total is|the count is|campus has) ({COUNTS}) (?:on campus|in total|altogether)\b", count_text)
    source = words(" ".join(c["text"] for c in chunks))
    for count in totals:
        if not re.search(rf"\b(?:there (?:are|is)|a total of|campus has|university has|only) {count} (?:campus )?book(?:store|shop)s?\b", source):
            return False
    qualified = re.search(r"cannot|can t|could not|unknown|unverified|unable|no (?:exact |definitive |complete |verified )?(?:count|total)|not (?:(?:explicitly|clearly|fully) )?(?:specified|provided|stated|listed|confirmed|given|established)|does not|doesnt|do not|dont|isnt|arent|not available|not enough|at least", text)
    return bool(totals or qualified)


def check_unc(url, provider, sources, chunks, places, build=None):
    results = []
    for i, question in enumerate(UNC_QUESTIONS):
        answer, cited = cited_answer(url, question, provider, sources, build)
        relevant = [row for row in chunks if row["url"] in cited]
        if i == 0:
            good = enrollment_supported(answer, relevant)
        elif i == 1:
            good = bookstore_supported(answer, cited, relevant, places)
        else:
            venues = matched_places(answer, cited, relevant, places, "dining")
            answer_terms = set(words(answer).split())
            generic = {"the", "on", "at", "of", "near", "campus", "unc", "north", "carolina", "university"}
            good = len(venues) >= 2 or any(
                (set(words(v.get("address", "")).split()) - generic) & answer_terms for v in venues
            )
        if not good:
            raise ValueError(f"UNC acceptance failed for {question!r} with cited sources {sorted(cited)}: {answer[:600]}")
        results.append({"question": question, "answer": answer, "citations": sorted(cited)})
    return results


def check(url, customer, provider, build=None):
    for attempt in range(12):
        try:
            profile = json.loads(request(url + "/api/profile", build=build))
            break
        except OSError:
            if attempt == 11:
                raise
            time.sleep(3)
    if profile.get("customer_name") != customer or profile.get("provider") != provider:
        raise ValueError("Live customer identity or runtime does not match the selection")
    build = build or profile.get("route_build")
    chunks = [json.loads(line) for line in request(url + "/rag.jsonl", build=build).splitlines() if line.strip()]
    manifest = json.loads(request(url + "/api/sources", build=build))
    if build and not (profile.get("generated_at") == manifest.get("built_at") == build):
        raise ValueError("The live profile and source inventory belong to different builds")
    sources = inventory_check(profile, manifest, chunks)
    titles = list(dict.fromkeys(c["title"] for c in sorted(chunks, key=lambda c: -len(c["title"].split()))))
    questions = [profile.get("sample_question")] + ['What does the page "%s" say?' % t[:180] for t in titles[:2]]
    for question in filter(None, questions):
        try:
            _, cited = cited_answer(url, question, provider, sources, build)
        except ValueError:
            continue
        result = {"provider": provider, "url": url, "rag_url": url + "/rag.jsonl", "sources_url": url + "/api/sources",
                  "build": build, "pages": len(sources), "chunks": len(chunks), "question": question, "citations": sorted(cited)}
        host = (urlsplit(profile.get("customer_website", "")).hostname or "").lower()
        if provider in AI_PROVIDERS and host.removeprefix("www.") == "unc.edu":
            result["rehearsal"] = check_unc(url, provider, sources, chunks, manifest.get("places", []), build)
        return result
    raise ValueError("No verified answer from the selected provider with citations in the live corpus")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for field in ("url", "customer", "provider"):
        p.add_argument(field)
    p.add_argument("--build")
    args = p.parse_args()
    try:
        print(json.dumps(check(args.url, args.customer, args.provider, args.build)))
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as e:
        sys.exit(str(e))
