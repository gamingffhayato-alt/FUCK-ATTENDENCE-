"""
Groq integration — OCR-extracted timetable text -> day-by-day JSON.

The API call below is configured EXACTLY as specified:
    model="openai/gpt-oss-20b", temperature=1, max_completion_tokens=2048,
    top_p=1, reasoning_effort="medium", stream=True, stop=None
and streamed chunks are concatenated into the final JSON string.
(The only additions after the call: a guard that skips empty usage-only
chunks, and a tolerant json.loads so markdown fences cannot crash the app.)
"""

import os
import json
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
client = Groq()  # reads GROQ_API_KEY from .env


# Pass the OCR-extracted text from the uploaded image to this function
def analyze_daily_schedule(extracted_text):
    completion = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {
                "role": "system",
                "content": "You are a schedule analyzer. Read this raw OCR text of a weekly timetable and return ONLY a valid JSON object where keys are the days of the week (e.g., 'Monday', 'Tuesday') and values are arrays of subject names scheduled for that day."
            },
            {
                "role": "user",
                "content": extracted_text
            }
        ],
        temperature=1,
        max_completion_tokens=2048,
        top_p=1,
        reasoning_effort="medium",
        stream=True,
        stop=None
    )

    response_text = ""
    for chunk in completion:
        if not chunk.choices:  # skip trailing usage-only chunks
            continue
        response_text += chunk.choices[0].delta.content or ""

    # == spec's `return json.loads(response_text)`, hardened vs ```json fences
    text = response_text.strip()
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
        raise
