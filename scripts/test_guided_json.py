#!/usr/bin/env python3
import json
import sys
from openai import OpenAI

client = OpenAI(
    api_key="none",
    base_url="http://8.130.169.133:8088/v1",
    timeout=120,
)

test_text = "vLLM releases 0.7 with multi-LoRA support for efficient serving"

# === Test 1: Normal JSON (baseline) ===
print("=== Test 1: Normal JSON output ===")
resp = client.chat.completions.create(
    model="Qwen3.6-27B",
    messages=[
        {"role": "system", "content": "You are a classifier. Return valid JSON only."},
        {"role": "user", "content": f'Classify this article and return JSON:\nTitle: {test_text}'},
    ],
    temperature=0.0,
    max_tokens=128,
)
msg = resp.choices[0].message
print(f"content: {msg.content!r}")
print(f"refusal: {getattr(msg, 'refusal', 'N/A')}")
print(f"finish_reason: {resp.choices[0].finish_reason}")
if msg.content:
    cleaned = msg.content.strip()
    for fence in ["```json", "```"]:
        cleaned = cleaned.removeprefix(fence).removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
        print(f"OK: parsed {list(data.keys())}")
    except json.JSONDecodeError as e:
        print(f"Parse failed: {e}")

# === Test 2: guided_json via extra_body ===
print()
print("=== Test 2: guided_json via extra_body ===")
schema = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "is_relevant": {"type": "boolean"},
        "category": {
            "type": "string",
            "enum": ["LLM", "Agent", "AI for Science", "Design Simulation", "Digital Twin"]
        }
    },
    "required": ["reasoning", "is_relevant", "category"]
}
try:
    resp2 = client.chat.completions.create(
        model="Qwen3.6-27B",
        messages=[
            {"role": "system", "content": "Classify this article. Return JSON."},
            {"role": "user", "content": f"Title: {test_text}"},
        ],
        temperature=0.0,
        max_tokens=128,
        extra_body={"guided_json": json.dumps(schema)},
    )
    msg2 = resp2.choices[0].message
    print(f"content: {msg2.content!r}")
    print(f"refusal: {getattr(msg2, 'refusal', 'N/A')}")
    print(f"finish_reason: {resp2.choices[0].finish_reason}")
    if msg2.content:
        cleaned2 = msg2.content.strip()
        data2 = json.loads(cleaned2)
        print(f"OK: guided_json worked -> {data2}")
        print()
        print("RESULT: guided_json is SUPPORTED")
    else:
        print("FAILED: content is None even with guided_json")
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
    print()
    print("RESULT: guided_json is NOT supported (will use normal JSON fallback)")
