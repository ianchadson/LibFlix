#!/usr/bin/env python3
"""Low-volume semantic smoke benchmark for a deployed LibFlix instance."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TOPICS = (
    "focus", "meditation", "startups", "productivity", "habits", "sleep",
    "anxiety", "depression", "mental health", "leadership", "management",
    "marketing", "sales", "investing", "finance", "economics", "psychology",
    "philosophy", "creativity", "writing", "communication", "relationships",
    "parenting", "health", "fitness", "nutrition", "technology",
    "artificial intelligence", "climate change", "science",
)

GOLDEN_TITLES = {
    "focus": ("deep work", "stolen focus", "indistractable", "hyperfocus"),
    "meditation": (
        "mindfulness in plain english", "waking up", "wherever you go",
        "miracle of mindfulness",
    ),
    "startups": (
        "lean startup", "zero to one", "hard thing about hard things",
        "founders at work", "traction",
    ),
}


def _fetch_topic_once(base_url: str, topic: str, timeout: float) -> dict:
    query = urlencode({"q": topic, "intent": "topic"})
    request = Request(
        f"{base_url.rstrip('/')}/api/discover?{query}",
        headers={"User-Agent": "LibFlix-Topic-Benchmark/1.0"},
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
            status = response.status
    except HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After", "0")
        try:
            payload = json.load(error)
        except (ValueError, TypeError):
            payload = {"success": False, "error": str(error)}
    except (URLError, TimeoutError, ValueError) as error:
        status = 0
        retry_after = "0"
        payload = {"success": False, "error": str(error)}
    else:
        retry_after = "0"
    elapsed = round(time.perf_counter() - started, 3)
    start_here = payload.get("start_here") if isinstance(payload, dict) else []
    explore = payload.get("books") if isinstance(payload, dict) else []
    books = [book for book in (*list(start_here or []), *list(explore or [])) if isinstance(book, dict)]
    keys = [book.get("ol_key") for book in books if book.get("ol_key")]
    titles = [str(book.get("title") or "").casefold() for book in books]
    cover_count = sum(bool(book.get("cover_url")) for book in books)
    reasoned_count = sum(bool(book.get("reasons") or book.get("reason")) for book in books)
    golden = GOLDEN_TITLES.get(topic)
    golden_match = None if golden is None else any(
        expected in title for expected in golden for title in titles
    )
    return {
        "topic": topic,
        "status": status,
        "success": bool(payload.get("success")) if isinstance(payload, dict) else False,
        "elapsed_seconds": elapsed,
        "partial": bool(payload.get("partial")) if isinstance(payload, dict) else False,
        "sources": payload.get("sources", []) if isinstance(payload, dict) else [],
        "books": len(books),
        "unique_books": len(set(keys)),
        "cover_rate": round(cover_count / len(books), 3) if books else 0.0,
        "reasoned_rate": round(reasoned_count / len(books), 3) if books else 0.0,
        "golden_match": golden_match,
        "titles": [book.get("title") for book in books[:10]],
        "error": payload.get("error") if isinstance(payload, dict) else "Invalid response",
        "retry_after": max(
            max(0, int(retry_after)) if str(retry_after).isdigit() else 0,
            max(0, int(payload.get("retry_after") or 0))
            if isinstance(payload, dict)
            and str(payload.get("retry_after") or 0).isdigit()
            else 0,
        ),
    }


def fetch_topic(
    base_url: str,
    topic: str,
    timeout: float,
    *,
    retries: int = 2,
    retry_delay: float = 18.0,
) -> dict:
    result = {}
    for attempt in range(max(0, retries) + 1):
        result = _fetch_topic_once(base_url, topic, timeout)
        result["attempts"] = attempt + 1
        retryable = (
            result["status"] in {0, 429, 503}
            or (result["partial"] and result["books"] == 0)
        )
        if not retryable or attempt >= retries:
            break
        time.sleep(max(float(result.get("retry_after") or 0), retry_delay))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://libflix.fomalhaut.app")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--delay", type=float, default=18.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=18.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    results = []
    for index, topic in enumerate(TOPICS):
        if index and args.delay > 0:
            time.sleep(args.delay)
        result = fetch_topic(
            args.base_url,
            topic,
            args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
        results.append(result)
        print(
            f"{topic:24} status={result['status']:3} books={result['books']:2} "
            f"covers={result['cover_rate']:.0%} partial={result['partial']} "
            f"time={result['elapsed_seconds']:.2f}s",
            flush=True,
        )

    successful = [result for result in results if result["success"]]
    populated = [result for result in successful if result["unique_books"] >= 1]
    golden_failures = [
        result["topic"] for result in results if result["golden_match"] is False
    ]
    partial_count = sum(result["partial"] for result in results)
    average_cover_rate = round(
        sum(result["cover_rate"] for result in populated) / len(populated), 3
    ) if populated else 0.0
    average_reasoned_rate = round(
        sum(result["reasoned_rate"] for result in populated) / len(populated), 3
    ) if populated else 0.0
    minimum_populated = int(len(TOPICS) * 0.7)
    maximum_partial = int(len(TOPICS) * 0.5)
    summary = {
        "base_url": args.base_url,
        "topics": len(results),
        "successful": len(successful),
        "populated": len(populated),
        "partial": partial_count,
        "maximum_partial": maximum_partial,
        "minimum_populated": minimum_populated,
        "average_elapsed_seconds": round(
            sum(result["elapsed_seconds"] for result in results) / len(results), 3
        ),
        "average_cover_rate": average_cover_rate,
        "average_reasoned_rate": average_reasoned_rate,
        "golden_failures": golden_failures,
        "results": results,
    }
    serialized = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)

    if not args.strict:
        return 0
    return int(
        len(populated) < minimum_populated
        or partial_count > maximum_partial
        or average_cover_rate < 0.5
        or average_reasoned_rate < 0.7
        or bool(golden_failures)
    )


if __name__ == "__main__":
    sys.exit(main())
