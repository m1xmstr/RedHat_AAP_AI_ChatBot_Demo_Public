#!/usr/bin/env python3
"""Build a small branded EDU chatbot artifact from public website content."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from edu_retrieval import PassageIndex, make_chunks, words
from build_sources import write_sources
from public_crawl import crawl_site, normalize_url


DEFAULT_QUESTIONS = [
    "When is open enrollment?",
    "When can I register for classes?",
    "How much is tuition going up?",
    "How do I apply for financial assistance?",
    "Where are the closest bookstores to campus?",
    "How many students find job placement after graduation?",
]

def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "edu-demo"


def file_to_data_uri(path: str) -> str:
    if not path:
        return ""
    file_path = Path(path).resolve()
    allowed = Path(__file__).resolve().parents[1] / "assets" / "logos"
    if allowed.resolve() not in file_path.parents:
        raise ValueError("Logo file must be under assets/logos")
    if file_path.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg", ".svg", ".gif", ".ico"}:
        raise ValueError("Unsupported logo format")
    if not file_path.exists():
        raise FileNotFoundError(f"Logo file not found: {file_path}")
    if file_path.stat().st_size > 500_000:
        raise ValueError("Logo exceeds 500 KB")
    mime = mimetypes.guess_type(str(file_path))[0] or "image/png"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>EDU AI Demo</title>
  <link rel="icon" href="/favicon.ico" type="image/vnd.microsoft.icon" sizes="16x16 32x32 48x48" />
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body>
  <main class="shell">
    <section class="brand-panel">
      <div class="brand-row"><img id="logo" class="logo" alt="" /><span class="demo-flag">Demo</span></div>
      <h1 id="customer">EDU AI Demo</h1>
      <p id="website" class="site"></p>
      <p id="runtime" class="runtime"></p>
      <p id="coverage" class="site"></p>
      <div class="source-links"><a href="/sources">View indexed sources</a><a href="/rag.jsonl" download>Download RAG corpus (JSONL)</a><a id="map-link" hidden target="_blank" rel="noreferrer">Find on Google Maps</a></div>
    </section>
    <section class="chat-panel">
      <div id="messages" class="messages" role="log" aria-live="polite"></div>
      <div id="suggestions" class="suggestions"></div>
      <form id="chat-form" class="composer">
        <input id="question" aria-label="Your question" maxlength="2000" autocomplete="off" placeholder="Ask a question about this organization" />
        <button type="submit">Ask</button>
      </form>
    </section>
  </main>
  <script>
    let profile = {};
    let previousQuestion = "";
    function browserSession() {
      const token = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        byte => byte.toString(16).padStart(2, "0")).join("");
      try {
        const stored = sessionStorage.getItem("demo-chat-session");
        if (stored && /^[0-9a-f]{32}$/.test(stored)) return stored;
        sessionStorage.setItem("demo-chat-session", token);
      } catch {
        // With browser storage disabled, use a session valid for this page load.
      }
      return token;
    }
    const session = browserSession();
    const messages = document.querySelector("#messages");
    const suggestions = document.querySelector("#suggestions");
    const form = document.querySelector("#chat-form");
    const question = document.querySelector("#question");

    function addMessage(role, text, sources, method, places = []) {
      const item = document.createElement("article");
      item.className = `message ${role}`;
      const message = document.createElement("p");
      message.textContent = text;
      item.appendChild(message);
      if (method) {
        const label = document.createElement("small");
        label.className = "method";
        label.textContent = method;
        item.appendChild(label);
      }
      if (sources && sources.length) {
        const list = document.createElement("div");
        list.className = "sources";
        sources.slice(0, 4).forEach((src) => {
          const a = document.createElement("a");
          a.href = src.url;
          a.target = "_blank";
          a.rel = "noreferrer";
          a.textContent = src.title || src.url;
          list.appendChild(a);
        });
        item.appendChild(list);
      }
      if (places.length) {
        const maps = document.createElement("div");
        maps.className = "sources";
        places.slice(0, 3).forEach((place) => {
          if (!place.maps_url || !place.maps_url.startsWith("https://www.google.com/maps/search/?")) return;
          const link = document.createElement("a");
          link.href = place.maps_url;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.textContent = "Map · " + place.name;
          maps.appendChild(link);
        });
        if (maps.childElementCount) item.appendChild(maps);
      }
      messages.appendChild(item);
      messages.scrollTop = messages.scrollHeight;
    }

    async function ask(text) {
      addMessage("user", text);
      const controls = [...form.elements, ...suggestions.querySelectorAll("button")];
      controls.forEach((control) => control.disabled = true);
      addMessage("assistant", "Searching indexed sources…");
      const pending = messages.lastElementChild;
      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Demo-Session": session},
          body: JSON.stringify({question: text, previous_question: previousQuestion})
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Request failed");
        let method = platformNames[data.provider] || data.provider;
        if (data.provider === "evidence_fallback") method = "Model unavailable · showing a retrieved passage";
        if (data.method === "source_selection") method += " · selected sources";
        previousQuestion = text;
        pending.remove();
        addMessage("assistant", data.answer, data.sources || [], method, data.places || []);
      } catch (error) {
        pending.remove();
        addMessage("assistant", "The request failed. You can retry your question.");
        question.value = text;
      } finally {
        controls.forEach((control) => control.disabled = false);
        question.focus();
      }
    }

    const platformNames = {openshift_ai: "OpenShift AI", rhel_ai: "RHEL AI", rhel_vm: "Red Hat AI Inference on RHEL", openshift: "OpenShift", podman: "Podman on RHEL", artifact: "Local artifact", evidence: "Source retrieval"};

    async function boot() {
      profile = await (await fetch("/api/profile")).json();
      document.title = `${profile.customer_name} AI Demo`;
      document.querySelector("#customer").textContent = `${profile.customer_name} AI Assistant`;
      document.querySelector("#website").textContent = profile.customer_website;
      if (profile.map_url) {
        const map = document.querySelector("#map-link");
        map.href = profile.map_url;
        map.hidden = false;
      }
      document.querySelector("#coverage").textContent = `${profile.chunk_count} passages from ${profile.page_count} public pages`;
      document.querySelector("#runtime").textContent = (platformNames[profile.provider] || profile.provider) + (profile.model_configured ? " · Model-backed chat" : " · Source retrieval");
      if (profile.logo_url) {
        document.querySelector("#logo").onerror = (event) => event.target.hidden = true;
        document.querySelector("#logo").src = profile.logo_url;
      } else {
        document.querySelector("#logo").style.display = "none";
      }
      addMessage("assistant", `Ask about ${profile.customer_name}. Answers use the indexed public pages and include links to the sources.`);
      (profile.suggestions || [profile.sample_question]).filter(Boolean).forEach((q) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = q;
        button.onclick = () => ask(q);
        suggestions.appendChild(button);
      });
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = question.value.trim();
      if (!text) return;
      question.value = "";
      ask(text);
    });

    boot().catch(() => addMessage("assistant", "The demo profile could not load. Refresh to retry."));
  </script>
</body>
</html>
"""


STYLES_CSS = """
:root {
  color-scheme: dark;
  --bg: #08111f;
  --panel: #101c2d;
  --line: #27415f;
  --text: #eaf3ff;
  --muted: #aebfd3;
  --accent: #35d0a2;
  --accent-2: #7cc8ff;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: radial-gradient(circle at 25% 20%, rgba(53, 208, 162, .16), transparent 28%), var(--bg);
  color: var(--text);
}
.shell {
  display: grid;
  grid-template-columns: minmax(280px, 380px) 1fr;
  gap: 24px;
  width: min(1180px, calc(100vw - 32px));
  min-height: calc(100vh - 32px);
  margin: 16px auto;
}
.brand-panel, .chat-panel {
  border: 1px solid var(--line);
  background: rgba(16, 28, 45, .92);
  border-radius: 8px;
}
.brand-panel {
  padding: 28px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.logo {
  display: block;
  max-width: min(70%, 220px);
  max-height: 120px;
  object-fit: contain;
  background: #fff;
  padding: 12px;
  border-radius: 6px;
  margin: 0;
  color-scheme: light;
}
.brand-row {display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:24px;}
.demo-flag {border:1px solid var(--line); padding:4px 10px; border-radius:20px; color:var(--muted); font-size:12px;}
.source-links {display:grid; gap:10px;}
.source-links a,.source-page a {color:var(--accent-2);}
.source-page {max-width:850px; margin:32px auto; padding:20px;}
.source-page h1 {margin-top:24px;}
.source-page ul {padding:0; list-style:none;}
.source-page li {padding:12px 0; border-bottom:1px solid var(--line);}
.source-page small {display:block; color:var(--muted); overflow-wrap:anywhere;}
h1 {
  margin: 0;
  font-size: 42px;
  line-height: 1.02;
}
.site {
  color: var(--muted);
  margin: 16px 0 26px;
  word-break: break-word;
}
.download, .method {color: var(--accent-2);}
.method {display: block; margin-top: .6rem;}
.message p {white-space: pre-line;}
.runtime {font-weight: 700; margin: 0;}
.logo[hidden] {display:none;}
.suggestions button {
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--text);
  background: #0b1728;
  padding: 8px 10px;
  font-weight: 700;
  font-size: 13px;
}
.chat-panel {
  display: grid;
  grid-template-rows: 1fr auto auto;
  min-height: 620px;
}
.messages {
  padding: 24px;
  overflow: auto;
}
.message {
  max-width: 820px;
  padding: 16px 18px;
  margin: 0 0 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.message p {
  margin: 0;
  line-height: 1.55;
}
.message.user {
  margin-left: auto;
  background: #173b53;
}
.message.assistant {
  background: #0b1728;
}
.sources {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}
.sources a {
  color: var(--accent-2);
  overflow-wrap: anywhere;
}
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0 24px 16px;
}
.suggestions button {
  cursor: pointer;
}
.composer {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  padding: 16px;
  border-top: 1px solid var(--line);
}
.composer input, .composer button {
  min-height: 48px;
  border-radius: 6px;
  border: 1px solid var(--line);
  font: inherit;
}
.composer input {
  width: 100%;
  background: #07101d;
  color: var(--text);
  padding: 0 14px;
}
.composer button {
  background: var(--accent);
  color: #03150f;
  font-weight: 900;
  padding: 0 22px;
  cursor: pointer;
}
@media (max-width: 800px) {
  .shell {
    grid-template-columns: 1fr;
  }
  h1 {
    font-size: 34px;
  }
  .chat-panel {
    min-height: 560px;
  }
}
"""


CONTAINERFILE = """FROM registry.access.redhat.com/ubi9/python-311:latest
WORKDIR /opt/app
COPY . /opt/app
EXPOSE 8080
ENV PORT=8080
CMD ["python", "/opt/app/app.py"]
"""


def write_app(output_dir: Path, profile: dict[str, object], knowledge: dict[str, object], chunks: list[dict]) -> None:
    app_dir = output_dir / "app"
    static_dir = app_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("static") / "favicon.ico", static_dir / "favicon.ico")
    shutil.copyfile(Path(__file__).with_name("demo_server.py"), app_dir / "app.py")
    shutil.copyfile(Path(__file__).with_name("edu_retrieval.py"), app_dir / "edu_retrieval.py")
    shutil.copyfile(Path(__file__).with_name("places.py"), app_dir / "places.py")
    with (app_dir / "rag.jsonl").open("w", encoding="utf-8") as output:
        for row in chunks:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    (app_dir / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (app_dir / "knowledge.json").write_text(json.dumps(knowledge, indent=2), encoding="utf-8")
    (static_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (static_dir / "styles.css").write_text(STYLES_CSS, encoding="utf-8")
    (app_dir / "Containerfile").write_text(CONTAINERFILE, encoding="utf-8")


def write_report(output_dir: Path, profile: dict[str, object], knowledge: dict[str, object], answers: list[dict[str, object]], errors: list[str]) -> None:
    lines = [
        f"# {profile['customer_name']} EDU AI Demo Report",
        "",
        f"- Website: {profile['customer_website']}",
        f"- Pages crawled: {len(knowledge['pages'])}",
        f"- RAG passages: {profile['chunk_count']}",
        "- Downloadable corpus: app/rag.jsonl",
        "",
        "## Preview Answers",
        "",
    ]
    for answer in answers:
        lines.extend(
            [
                f"### {answer['question']}",
                "",
                textwrap.shorten(str(answer["answer"]), width=700, placeholder=" ..."),
                "",
            ]
        )
        for source in answer.get("sources", [])[:4]:
            lines.append(f"- [{source.get('title') or source.get('url')}]({source.get('url')})")
        lines.append("")
    if errors:
        lines.extend(["## Crawl Notes", ""])
        for error in errors[:20]:
            lines.append(f"- {error}")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def read_questions(path: str) -> list[str]:
    if not path:
        return DEFAULT_QUESTIONS
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(item) for item in data]
    raise ValueError("questions file must contain a JSON list")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-name", required=True)
    parser.add_argument("--customer-website", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--logo-url", default="")
    parser.add_argument("--logo-file", default="")
    parser.add_argument("--max-pages", type=int, default=64)
    parser.add_argument("--questions-file", default="")
    parser.add_argument("--seed-url", nargs="*", default=[])
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    questions = read_questions(args.questions_file)
    crawl = crawl_site(args.customer_website, args.seed_url, args.max_pages)
    pages = crawl.pages
    from places import customer_location, customer_map_url, extract_places, place_pages
    locations = extract_places(pages, args.customer_name, args.customer_website)
    chunks_by_id = {row["id"]: row for row in make_chunks([*pages, *place_pages(locations)])}
    chunks = list(chunks_by_id.values())
    _, customer_place = customer_location(locations, args.customer_name, args.customer_website)
    index = PassageIndex(chunks, customer_place)
    logo_url = (normalize_url(args.logo_url) if args.logo_url else "") or (file_to_data_uri(args.logo_file) if args.logo_file else crawl.logo_url)
    # Offer one question derived from an actual indexed source even for a new customer.
    sample = next((p for p in pages if "tuition" in p["title"].lower()), None)
    if sample is None:
        sample = next((p for p in pages if len(p["title"].split()) >= 3), pages[0])
    topic = sample["title"].split(" < ")[0].split(" | ")[0].strip()
    if not (set(words(topic)) - set(words(args.customer_name)) - {"home", "homepage"}):
        line = max(sample["text"].splitlines(), key=lambda line: min(len(words(line)), 12))
        topic = " ".join(line.split()[:10]).rstrip(".?!")
    profile = {
        "customer_name": args.customer_name,
        "customer_slug": slugify(args.customer_name),
        "customer_website": normalize_url(args.customer_website),
        "logo_url": logo_url,
        "page_count": len(pages),
        "chunk_count": len(chunks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_question": "What does the website say about " + topic + "?",
        "questions": questions,
        "provider": os.environ.get("DEMO_AI_PROVIDER", "artifact"),
        "generated_by": "EDU_AI_DEMO",
    }
    # Offer questions only when a named source or extracted venue covers their subject.
    titles = " ".join((page["title"] + " " + page["url"]) for page in pages).lower()
    categories = {location["category"] for location in locations}
    suggestions = []
    if re.search(r"registration|enrollment.calendar|registrar", titles):
        suggestions.append("When can I register for classes?")
    if "bookstore" in categories or re.search(r"bookstore|student.store", titles):
        suggestions.append("Where is the campus bookstore?")
    if "dining" in categories or re.search(r"dining|restaurants|where.to.eat", titles):
        suggestions.append("Where can I eat?")
    if re.search(r"financial.aid|student.aid", titles):
        suggestions.append("How do I apply for financial aid?")
    if re.search(r"our.services|what.we.do", titles):
        suggestions.append("What services does this organization offer?")
    profile["suggestions"] = list(dict.fromkeys([*suggestions, profile["sample_question"]]))[:5]
    profile["map_url"] = customer_map_url(locations, args.customer_name, args.customer_website)
    knowledge = {
        "customer_name": args.customer_name,
        "source_website": normalize_url(args.customer_website),
        "pages": pages,
    }
    answers = [index.evidence(question, args.customer_name)[0] for question in questions]

    (output_dir / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (output_dir / "knowledge.json").write_text(json.dumps(knowledge, indent=2), encoding="utf-8")
    (output_dir / "answers_preview.json").write_text(json.dumps(answers, indent=2), encoding="utf-8")
    write_app(output_dir, profile, knowledge, chunks)
    write_sources(output_dir / "app", profile, pages, chunks, crawl.errors, locations)
    write_report(output_dir, profile, knowledge, answers, crawl.errors)
    print(json.dumps({"output_dir": str(output_dir), "pages": len(pages), "chunks": len(chunks)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
