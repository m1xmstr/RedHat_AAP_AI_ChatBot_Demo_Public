"""Build and rank source-tagged passages for both the downloadable corpus and chat."""

import collections
import hashlib
import math
import re

STOP = set("a an and are as at after before be by can could do does find for from give going how i in is it many me much my of on or please say says should tell that the their there this to up what when where which who why will with would about website".split())
ALIAS = {
    "apply": "application", "applying": "application", "assistance": "aid",
    "register": "registration", "registering": "registration", "enroll": "registration",
    "enrollment": "registration", "enrolment": "registration", "enrolling": "registration",
    "classes": "class", "dates": "date", "starts": "start", "begins": "begin",
    "bookshop": "bookstore", "bookshops": "bookstore", "bookstores": "bookstore",
    "food": "dining", "eat": "dining", "eating": "dining", "restaurant": "dining",
    "restaurants": "dining", "cafeteria": "dining", "dine": "dining", "meals": "dining",
    "shops": "shop", "scholarships": "scholarship",
}
BOOK = re.compile(r"\b(?:book\s?(?:store|shop)s?|(?:student|campus) stores?)\b", re.I)
FOOD = re.compile(r"\b(?:dining|restaurants?|caf[eé]s?|eater(?:y|ies)|food|eat|meal(?:s| plan)?|cafeterias?)\b", re.I)
REGISTER = re.compile(r"\b(?:regist(?:er|ering|ration)|enroll?(?:ment|ing)?|enrolment)\b", re.I)
DATE = re.compile(r"\b(?:20\d\d-\d\d-\d\d|(?:jan(?:uary)?|feb(?:ruary)?|march|april|may|june|july|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2})\b", re.I)
COUNT = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+(?:on[- ]campus\s+|campus\s+)?(?:bookshops?|bookstores?)\b", re.I)


def explicit_book_count(line):
    """Only a statement about the campus-wide inventory can supply an exact count."""
    amount = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})"
    stores = r"(?:on[- ]campus |campus )?(?:bookshops?|bookstores?)(?: locations?)?"
    assertion = rf"(?:campus|university|school) (?:has|operates|maintains) (?:a total of )?{amount} {stores}\b|(?:there (?:are|is)|we (?:have|operate)) (?:a total of )?{amount} {stores} (?:on (?:the |our )?campus|at (?:this |our |the )?(?:university|school))"
    return bool(re.search(assertion, line, re.I))


def words(text):
    result = []
    lower = text.lower()
    if re.search(r"\b(?:student|campus) stores?\b|\bbook stores?\b", lower):
        result.append("bookstore")
    for word in re.findall(r"[a-z0-9]{2,}", lower):
        if word in STOP:
            continue
        word = ALIAS.get(word, word)
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith(("ss", "us")) and len(word) > 4:
            word = word[:-1]
        result.append(word)
    return result


def sections(page):
    section, lines, size = "", [], 0
    headings = set(page.get("headings", []))
    for line in page["text"].splitlines():
        if page.get("format") == "ical":
            if line.strip():
                yield page["title"], line
            continue
        if line in headings or (lines and size + len(line) > 1100):
            if lines:
                yield section, "\n".join(lines)
            lines, size = [], 0
        if line in headings:
            section = line[:180]
        # A minified source can contain a whole article on one line.
        for part in re.findall(r".{1,1100}(?:\s+|$)|\S{1,1100}", line):
            part = part.strip()
            if not part:
                continue
            if size + len(part) > 1300 and lines:
                yield section, "\n".join(lines)
                lines, size = [], 0
            lines.append(part)
            size += len(part) + 1
    if lines:
        yield section, "\n".join(lines)


def make_chunks(pages):
    seen = set()
    for page in pages:
        for section, passage in sections(page):
            if len(passage.strip()) < 15:
                continue
            digest = hashlib.sha256((page["url"] + "\n" + section + "\n" + passage).encode()).hexdigest()[:20]
            if digest in seen:
                continue
            seen.add(digest)
            yield {"id": digest, "title": page["title"], "url": page["url"], "text": passage,
                   "section": section, "format": page.get("format", "html"), "topics": page.get("topics", [])}


class PassageIndex:
    def __init__(self, rows):
        self.rows = list(rows)
        self.frequencies = []
        self.titles = []
        self.document_count = collections.Counter()
        for row in self.rows:
            freq = collections.Counter(words(row["text"]))
            title = set(words(row["title"].split(" < ")[0].split(" | ")[0] + " " + row.get("section", "")))
            self.frequencies.append(freq)
            self.titles.append(title)
            self.document_count.update(freq.keys() | title)
        self.average_length = sum(sum(f.values()) for f in self.frequencies) / max(1, len(self.rows))

    def search(self, question, customer_name="", limit=4):
        query = set(words(question)) - set(words(customer_name))
        if not query:
            return []
        book, food, registration = bool(BOOK.search(question)), bool(FOOD.search(question)), bool(REGISTER.search(question))
        if book or food or registration:
            query -= {"campus", "location", "place", "near", "nearby", "closest", "nearest", "class", "get", "school"}
        broad_registration = registration and query <= {"registration", "open", "date", "start", "begin", "course", "spring", "summer", "fall", *re.findall(r"20\d\d", question)}
        explicit_term = re.search(r"\b(spring|fall|summer|winter)\s+(20\d\d)\b", question, re.I)
        years = set(re.findall(r"\b20\d\d\b", question))
        periods = set(re.findall(r"\b(?:spring|fall|summer|winter)\b", question.lower()))
        ranked = []
        for row, freq, title in zip(self.rows, self.frequencies, self.titles):
            body, heading = row["text"], row["title"] + " " + row.get("section", "")
            relevant = heading + "\n" + body
            if book and not BOOK.search(relevant) or food and not FOOD.search(relevant):
                continue
            if registration and not REGISTER.search(relevant):
                continue
            if registration and years and not any(year in relevant for year in years):
                continue
            if explicit_term and row.get("format") == "ical" and explicit_term[0].lower() not in heading.lower():
                continue
            if periods and row.get("format") == "ical" and not any(p in heading.lower() for p in periods):
                continue
            hits = query & (freq.keys() | title)
            if not hits or len(hits) < len(query) * .5:
                continue
            size = sum(freq.values()) / max(1, self.average_length)
            score = 0.0
            for term in hits:
                count = freq[term]
                rarity = math.log(1 + (len(self.rows) - self.document_count[term] + .5) / (self.document_count[term] + .5))
                score += rarity * (2.2 * count / (count + .4 + .8 * size) + (1.1 if term in title else 0))
            if book:
                score += 3 * bool(BOOK.search(body)) + 2 * bool(re.search(r"bookshop|bookstore|book store", body, re.I))
                score += 3 * bool(re.search(r"\blocated\b|\baddress\b", body, re.I)) + 6 * bool(COUNT.search(body))
            if food:
                if query <= {"dining", "option", "meal", "buy", "lunch", "dinner"}:
                    score += 7 * bool(re.search(r"/(?:locations|restaurants|dining-halls|places-to-eat)(?:/|$)", row["url"], re.I))
                    score += 8 * body.startswith("Dining location:")
                    score += 4 * bool(re.search(r"located|situated|serv(?:es|ing)|offers", body, re.I))
                    if re.search(r"share .+ on facebook|follow place|food security month|calendar of events", body, re.I):
                        score -= 10
                score += 3 * ("dining" in row.get("topics", [])) + 2 * bool(re.search(r"dining hall|restaurant|caf[eé]", body, re.I))
            if broad_registration:
                low = body.lower()
                score += 5 * bool(DATE.search(body))
                score += 5 * bool(re.search(r"registration begins|registration opens|enrollment begins|enrollment opens", low))
                score += 8 * bool(re.search(r"open enrollment begins|open registration begins", low))
                score += 5 * bool(re.search(r"wave \d of undergraduate registration begins|registration begins for all graduate", low))
                if any(x in low for x in ("late registration", "dual enrollment", "registration closes", "withdrawal", "enrollment deposit")):
                    score -= 6
            if score > 0:
                ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        matches = []
        source_count = collections.Counter()
        deferred = []
        for _, row in ranked:
            # Multiple event rows represent distinct cohorts/dates in the same calendar.
            if source_count[row["url"]] >= 2 and row.get("format") != "ical":
                deferred.append(row)
                continue
            matches.append(row)
            source_count[row["url"]] += 1
            if len(matches) >= limit:
                break
        return (matches + deferred)[:limit]

    def evidence(self, question, customer_name):
        passages = self.search(question, customer_name)
        if not passages:
            return {"question": question, "answer": f"I could not verify that from the indexed pages for {customer_name}.",
                    "confidence": "not_found", "sources": [], "provider": "evidence"}, []
        query = set(words(question))
        lines = re.split(r"\n|(?<=[.!?])\s+", passages[0]["text"])
        def line_score(i):
            line = lines[i]
            terms = len(set(words(line)) & query)
            if BOOK.search(question):
                terms += 6 * bool(BOOK.search(line)) + 5 * bool(BOOK.search(line) and re.search(r"located|building|address", line, re.I))
            return terms, min(len(line), 180)
        best = max((i for i, line in enumerate(lines) if line.strip()), key=line_score)
        excerpt = " ".join(lines[max(0, best - (1 if len(lines[best]) < 80 else 0)):best+2])
        answer = "Related passage from the public website:\n" + excerpt[:1400]
        if passages[0].get("format") == "ical":
            events = [p["text"] for p in passages if p.get("format") == "ical" and p["url"] == passages[0]["url"]]
            answer = passages[0]["title"] + "\n" + "\n".join(events)
        if FOOD.search(question) and any(p["text"].startswith("Dining location:") for p in passages):
            places = [line for p in passages for line in p["text"].splitlines() if line.startswith("Dining location:")]
            answer = "\n".join(dict.fromkeys(places[:4]))
        elif FOOD.search(question) and passages[0].get("section"):
            first = passages[0]
            answer = first["section"] + "\n" + first["text"].removeprefix(first["section"]).strip()[:600]
        if BOOK.search(question) and re.search(r"\b(?:how many|number of|count)\b", question, re.I) and not any(explicit_book_count(p["text"]) for p in passages):
            answer = "These sources identify bookstore locations but do not establish the total number on campus.\n" + excerpt[:1100]
        sources = []
        for row in passages:
            if not any(src["url"] == row["url"] for src in sources):
                sources.append({"title": row["title"], "url": row["url"]})
        return {"question": question, "answer": answer, "confidence": "evidence_found", "sources": sources, "provider": "evidence"}, passages
