"""Chunks extracted page text and asks Claude to structure each chunk into
hierarchical legislative components, using a JSON-schema-constrained output
so the result is always valid, then stitches chunks back together."""
import json

import anthropic

from .examples_store import load_examples
from .extract import PageText
from .prompts import build_system_prompt
from .schema import NODE_SCHEMA

MODEL = "claude-opus-5"
MAX_TOKENS = 16000


def chunk_pages(pages: list[PageText], pages_per_chunk: int = 8) -> list[list[PageText]]:
    return [pages[i : i + pages_per_chunk] for i in range(0, len(pages), pages_per_chunk)]


def render_chunk_text(pages_chunk: list[PageText]) -> str:
    return "\n\n".join(f"<<<PAGE {p.page_no}>>>\n{p.body}" for p in pages_chunk)


def parse_chunk(client: anthropic.Anthropic, chunk_text: str, system_prompt: str) -> list[dict]:
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": NODE_SCHEMA}},
        messages=[{"role": "user", "content": f"Text to structure:\n\n{chunk_text}"}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["nodes"]


def merge_nodes(all_chunk_nodes: list[list[dict]]) -> list[dict]:
    merged: list[dict] = []
    for nodes in all_chunk_nodes:
        for i, node in enumerate(nodes):
            can_merge = (
                i == 0
                and node.get("continued_from_previous")
                and merged
                and merged[-1].get("continues_in_next")
                and merged[-1]["type"] == node["type"]
            )
            if can_merge:
                prev = merged[-1]
                prev["text"] = (prev["text"].rstrip() + "\n" + node["text"].lstrip()).strip()
                prev["continues_in_next"] = node.get("continues_in_next", False)
                prev["page_end"] = node.get("page_end", prev.get("page_end"))
            else:
                merged.append(node)
    return merged


def structure_act(pages: list[PageText], act_name: str, pages_per_chunk: int = 8, verbose: bool = True) -> list[dict]:
    client = anthropic.Anthropic()
    examples = load_examples(k=6)
    system_prompt = build_system_prompt(examples)
    chunks = chunk_pages(pages, pages_per_chunk)

    all_nodes = []
    for idx, chunk in enumerate(chunks, 1):
        text = render_chunk_text(chunk)
        nodes = parse_chunk(client, text, system_prompt)
        all_nodes.append(nodes)
        if verbose:
            print(f"[{act_name}] chunk {idx}/{len(chunks)} (pages {chunk[0].page_no}-{chunk[-1].page_no}) -> {len(nodes)} nodes")

    return merge_nodes(all_nodes)
