#!/usr/bin/env python3
"""Offline smoke test for generated EDU_AI_DEMO app bundles."""

from __future__ import annotations

import argparse
import json
import runpy
import sys
import textwrap
from pathlib import Path


def read_questions(path: str, profile: dict[str, object]) -> list[str]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [str(item) for item in data]
    return [str(item) for item in profile.get("questions", [])]


def write_markdown(path: Path, profile: dict[str, object], results: list[dict[str, object]]) -> None:
    lines = [f"# Smoke Test: {profile['customer_name']}", ""]
    for result in results:
        lines.extend(
            [
                f"## {result['question']}",
                "",
                f"Confidence: `{result['confidence']}`",
                "",
                textwrap.shorten(str(result["answer"]), width=700, placeholder=" ..."),
                "",
            ]
        )
        for source in result.get("sources", [])[:4]:
            lines.append(f"- [{source.get('title') or source.get('url')}]({source.get('url')})")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--questions-file", default="")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args()

    app_dir = Path(args.app_dir)
    profile = json.loads((app_dir / "profile.json").read_text(encoding="utf-8"))
    # Load exactly the index and retrieval function shipped in this customer bundle.
    sys.path.insert(0, str(app_dir.resolve()))
    app = runpy.run_path(str(app_dir / "app.py"))
    evidence_answer = app["evidence_answer"]
    rows = app["INDEX"].rows
    source_urls = {row["url"] for row in rows}
    if len(rows) != profile["chunk_count"] or len(source_urls) != profile["page_count"] or not rows:
        sys.exit("Downloaded corpus and profile counts disagree or are empty")
    questions = list(dict.fromkeys([profile["sample_question"], *read_questions(args.questions_file, profile)]))
    results = [evidence_answer(question) for question in questions]
    Path(args.output_json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_markdown(Path(args.output_md), profile, results)
    verified = [item for item in results if item["confidence"] == "evidence_found" and item["sources"]]
    if not verified or any(source["url"] not in source_urls for item in results for source in item["sources"]):
        sys.exit("No verifiable cited answer or a citation is absent from the corpus")
    print(json.dumps({"questions": len(results), "with_citations": len(verified)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
