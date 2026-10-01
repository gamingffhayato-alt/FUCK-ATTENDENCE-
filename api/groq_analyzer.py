"""
Groq integration — extracted timetable text -> day-by-day JSON.

The API call below matches the Groq playground configuration EXACTLY:
    model="qwen/qwen3.8-27b", temperature=0.6, max_completion_tokens=2048,
    top_p=0.95, reasoning_effort="default", stream=True, stop=None
and streamed chunks are concatenated into the final JSON string.
(Additions after the call: a guard that skips empty usage-only chunks,
and a tolerant JSON parser so markdown fences cannot crash the app.)
"""

import os
import json
import base64
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
client = Groq()


def analyze_daily_schedule(payload):
    system_prompt = (
        "You are a schedule analyzer. Extract the timetable data. "
        "You MUST return ONLY a flat JSON object where the root keys are exactly the days "
        "of the week ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday') "
        "and the values are simple arrays of strings (the subject names). "
        "DO NOT wrap the output in a 'schedule' or 'timetable' parent key."
    )

    # If payload is bytes, use the Vision content array. Otherwise, use standard text.
    if isinstance(payload, bytes):
        image_b64 = base64.b64encode(payload).decode("utf-8")
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the timetable from this image into the exact JSON format requested."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            }
        ]
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(payload)}
        ]

    # Model and parameters explicitly matching the screenshot configuration
    completion = client.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=messages,
        temperature=0.6,
        max_completion_tokens=2048,
        top_p=0.95,
        reasoning_effort="default",
        stream=True,
        stop=None
    )

    # Process the streamed chunks to form the final JSON string
    response_text = ""
    for chunk in completion:
        if not chunk.choices:  # Skip trailing usage-only chunks
            continue
        response_text += chunk.choices[0].delta.content or ""

    text = response_text.strip()

    # Tolerant JSON parser to safely extract the payload
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError(f"Could not parse the model output as JSON. Raw output: {text}")
