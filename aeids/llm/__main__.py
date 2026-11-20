"""Check the OpenRouter setup once the API key is in ``.env``.

    python -m aeids.llm check

Makes one small embedding call and one small chat call; prints models, vector size, tokens,
cost and latency. Sends only the fixed test sentences below, no document content.
"""

from __future__ import annotations

import argparse
import json
import sys

from aeids.config import get_settings
from aeids.llm.embedding import OpenRouterEmbedder
from aeids.llm.openrouter import LLMError, OpenRouterClient

TEST_TEXTS = ["Total net sales were 416,161 million US dollars.", "Net income increased."]


def check() -> int:
    settings = get_settings()
    try:
        with OpenRouterClient(settings) as client:
            emb = client.embed(TEST_TEXTS)
            print(f"embeddings ok: {emb.model} · {len(emb.vectors)} vectors of size "
                  f"{len(emb.vectors[0])} · {emb.usage.total_tokens} tokens · "
                  f"{emb.latency_ms} ms · cost {emb.usage.cost}")
            query = OpenRouterEmbedder.from_settings(settings, client).embed_query("revenue 2025")
            print(f"query embedding ok: size {len(query)} (with instruction)")
            chat = client.chat(
                [{"role": "system", "content": "Reply with JSON only."},
                 {"role": "user", "content": 'Return {"status": "ok"}.'}],
                max_tokens=50,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(chat.content)
            print(f"chat ok: {chat.model} · reply {parsed} · {chat.usage.prompt_tokens}+"
                  f"{chat.usage.completion_tokens} tokens · {chat.latency_ms} ms · "
                  f"cost {chat.usage.cost}")
    except LLMError as exc:
        print(f"error ({type(exc).__name__}): {exc.message}", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("error: the chat model did not return valid JSON", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aeids.llm")
    ap.add_argument("cmd", choices=["check"])
    ap.parse_args(argv)
    return check()


if __name__ == "__main__":
    sys.exit(main())
