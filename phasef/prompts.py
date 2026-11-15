"""Fixed prompts. READER is identical for every arm; only the context block differs. Recipe versions are part of the cache key."""
READER_RECIPE = "reader-v1.1"
EXTRACT_RECIPE = "extract-v1"
JUDGE_RECIPE = "judge-v1"

READER_SYSTEM = "You answer questions about a user's past conversations using only the context provided. Be accurate and concise."

READER_TEMPLATE = (
    "I will give you context drawn from past chat history between you and a user. Please answer the question based on "
    "the relevant information in it. First extract the relevant information, then reason over it (use the dates given "
    "and the current date when the question depends on time), then give the final answer on a last line starting "
    "with 'Answer:'. If the context does not contain the information needed to answer, say that the information is not "
    "available instead of guessing.\n\n"
    "Context:\n\n{context}\n\nCurrent date: {date}\nQuestion: {question}\nAnswer (step by step):"
)

EXTRACT_SYSTEM = (
    "You extract structured memory facts from chat transcripts. Output only valid JSON, no prose, no code fences."
)

EXTRACT_TEMPLATE = """Below are {n} chat sessions between a user and an AI assistant. Each has an id, a date, and numbered rounds [r0], [r1], ... (a user message followed by the assistant reply).

Process each session INDEPENDENTLY. Do not relate sessions to each other. Extract durable, specific facts that a later question might ask about: the user's personal details, possessions, habits, preferences, plans, events with dates, quantities, names, places, numbers, and values that may later change; and concrete facts the assistant stated that the user could ask about again (names, figures, recommendations, steps). Skip small talk. At most 15 facts per session; prefer specific details over generalities.

Each fact is a JSON object with exactly these fields:
- "subject": who or what the fact is about (use "user" for the user)
- "predicate": the attribute or relation, a short noun or verb phrase
- "object": the value, specific and complete (keep numbers and units)
- "valid_from": ISO date (YYYY-MM-DD) from which the fact holds if the text says or implies it (resolve relative dates such as "last Tuesday" against the session date), else null
- "valid_to": ISO date it stopped holding if stated, else null
- "modality": one of "asserted" (stated as true), "decided" (a decision or commitment), "proposed" (suggested, not accepted), "hypothetical" (if/what-if/wish), "question" (the user asked about it)
- "scope": a short tag such as "personal", "work", "health", "travel", "finance", "hobby", "general"
- "author": "user" if the user stated it, "assistant" if only the assistant stated it
- "round": the integer index of the round where it is stated
- "cue_keys": 3 to 6 short phrases or words a future question might use to ask about this fact, including synonyms and the category, not only words from the text

Output exactly one JSON object: {{"sessions": [{{"session_id": "<id>", "facts": [ ... ]}}, ...]}} with one entry per session, in the given order.

{sessions}
"""

JUDGE_SYSTEM = ("You are a careful grader. For each numbered item follow its instructions exactly. Output exactly one line per item, "
                "in the form '<number>: yes' or '<number>: no', and nothing else.")
