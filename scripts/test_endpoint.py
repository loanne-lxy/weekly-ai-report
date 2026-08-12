#!/usr/bin/env python3
"""Diagnose Qwen3.6-27B endpoint behavior."""
import json
from openai import OpenAI

client = OpenAI(
    api_key="none",
    base_url="http://8.130.169.133:8088/v1",
    timeout=120,
)

# Test various max_tokens to find the sweet spot
for mt in [32, 64, 256, 512, 1024]:
    print(f"\n=== max_tokens={mt} ===")
    resp = client.chat.completions.create(
        model="Qwen3.6-27B",
        messages=[
            {"role": "system", "content": "Reply briefly."},
            {"role": "user", "content": "Say hello in JSON: {\"msg\": \"hi\"}"},
        ],
        temperature=0.0,
        max_tokens=mt,
    )
    msg = resp.choices[0].message
    print(f"  finish_reason: {resp.choices[0].finish_reason}")
    print(f"  content: {msg.content!r}")
    if hasattr(msg, 'reasoning') and msg.reasoning:
        print(f"  reasoning: {msg.reasoning!r}")

# Check what models are available
print("\n=== Available models ===")
try:
    models = client.models.list()
    for m in models.data:
        print(f"  {m.id}")
except Exception as e:
    print(f"  Error: {e}")
