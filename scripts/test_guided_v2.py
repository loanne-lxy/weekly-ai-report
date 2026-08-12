#!/usr/bin/env python3
"""Test guided_json with reasoning model at adequate max_tokens."""
import json
from openai import OpenAI

client = OpenAI(
    api_key="none",
    base_url="http://8.130.169.133:8088/v1",
    timeout=120,
)

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

# Test guided_json with adequate tokens
print("=== guided_json max_tokens=256 ===")
resp = client.chat.completions.create(
    model="Qwen3.6-27B",
    messages=[
        {"role": "system", "content": "Classify this article. Return strict JSON."},
        {"role": "user", "content": "Title: vLLM releases 0.7 with multi-LoRA support for efficient LLM serving"},
    ],
    temperature=0.0,
    max_tokens=256,
    extra_body={"guided_json": json.dumps(schema)},
)
msg = resp.choices[0].message
print(f"finish_reason: {resp.choices[0].finish_reason}")
reasoning_text = getattr(msg, 'reasoning', None)
if reasoning_text:
    print(f"reasoning ({len(reasoning_text)} chars): {reasoning_text[:100]}...")
print(f"content: {msg.content!r[:300]}")

if msg.content:
    try:
        data = json.loads(msg.content.strip())
        print(f"Parsed: {data}")
        if isinstance(data.get("is_relevant"), bool):
            print("SUCCESS: guided_json enforced schema correctly")
        else:
            print("PARTIAL: JSON valid but schema not strictly enforced")
    except json.JSONDecodeError as e:
        print(f"Parse failed: {e}")

# Compare normal vs guided on same prompt
print("\n=== Normal JSON (max_tokens=512) ===")
resp2 = client.chat.completions.create(
    model="Qwen3.6-27B",
    messages=[
        {"role": "system", "content": "Classify. Return JSON with keys: reasoning, is_relevant (bool), category (one of: LLM, Agent, AI for Science, Design Simulation, Digital Twin)."},
        {"role": "user", "content": "Title: AlphaFold 3 predicts protein-DNA interactions with unprecedented accuracy"},
    ],
    temperature=0.0,
    max_tokens=512,
)
msg2 = resp2.choices[0].message
reasoning2 = getattr(msg2, 'reasoning', None)
if reasoning2:
    print(f"reasoning ({len(reasoning2)} chars): {reasoning2[:100]}...")
print(f"content: {msg2.content!r[:300]}")

print("\n=== Guided JSON (max_tokens=512) ===")
resp3 = client.chat.completions.create(
    model="Qwen3.6-27B",
    messages=[
        {"role": "system", "content": "Classify. Return JSON."},
        {"role": "user", "content": "Title: AlphaFold 3 predicts protein-DNA interactions with unprecedented accuracy"},
    ],
    temperature=0.0,
    max_tokens=512,
    extra_body={"guided_json": json.dumps(schema)},
)
msg3 = resp3.choices[0].message
reasoning3 = getattr(msg3, 'reasoning', None)
if reasoning3:
    print(f"reasoning ({len(reasoning3)} chars): {reasoning3[:100]}...")
print(f"content: {msg3.content!r[:300]}")
