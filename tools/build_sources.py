"""Write the source inventory shared by the customer UI and AAP job output."""
import collections
import html
import json


def write_sources(app, profile, pages, chunks, errors, places=None):
    counts = collections.Counter(chunk["url"] for chunk in chunks)
    rows = [{"title": page["title"], "url": page["url"], "passages": counts[page["url"]],
             "characters": len(page["text"]), "format": page.get("format", "html"), "topics": page.get("topics", [])} for page in pages if counts[page["url"]]]
    inventory = {"customer": profile["customer_name"], "built_at": profile["generated_at"],
                 "source_count": len(rows), "passage_count": len(chunks), "sources": rows,
                 "places": places or []}
    (app / "sources.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    esc = html.escape
    items = "".join(f'<li><a href="{esc(row["url"], quote=True)}">{esc(row["title"])}</a>'
                    f'<small>{esc(row["url"])} · {row["passages"]} passages</small></li>' for row in rows)
    locations = "".join(f'<li><a href="{esc(place["maps_url"], quote=True)}">{esc(place["name"])}</a>'
                        f'<small>{esc(place.get("address",""))} · <a href="{esc(place["source_url"], quote=True)}">Source</a></small></li>'
                        for place in places or [] if place.get("maps_url") and place.get("source_url"))
    html_page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sources · {esc(profile['customer_name'])}</title>
<link rel="icon" href="/favicon.ico" type="image/vnd.microsoft.icon" sizes="16x16 32x32 48x48">
<link rel="stylesheet" href="/static/styles.css"></head><body><main class="source-page">
<a href="/">Back to chat</a><h1>Indexed sources</h1><p>{esc(profile['customer_name'])}</p>
<p>{len(rows)} sources · {len(chunks)} passages · Built {esc(profile['generated_at'][:10])}</p>
<a href="/rag.jsonl" download>Download RAG corpus (JSONL)</a>
{'<h2>Locations</h2><ul>'+locations+'</ul>' if locations else ''}
<ul>{items}</ul></main></body></html>"""
    (app / "static" / "sources.html").write_text(html_page, encoding="utf-8")
    lines = [f"Indexed {len(rows)} sources and {len(chunks)} passages for {profile['customer_name']}."]
    for row in rows:
        title = " ".join(row["title"].split())
        lines.append(f"INDEXED  {row['passages']:>3} passages  {title[:110]}  {row['url']}")
    for place in places or []:
        lines.append(f"LOCATION  {place['name']}  {place.get('source_url','')}")
    lines.extend(f"SKIPPED  {' '.join(str(note).split())[:350]}" for note in errors[:45])
    print("\n".join(lines), flush=True)
