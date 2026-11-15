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

EXTRACT_V2_RECIPE = "extract-v2"
EXPAND_RECIPE = "expand-v1"

EXTRACT_V2_TEMPLATE = """Below are {n} chat sessions between a user and an AI assistant. Each has an id, a date, and numbered rounds [r0], [r1], ... (a user message followed by the assistant reply).

Process each session INDEPENDENTLY. Do not relate sessions to each other. Extract durable, specific facts that a later question might ask about: the user's personal details, possessions, habits, preferences, plans, events with dates, quantities, names, places, numbers and values that may later change; and concrete facts the assistant stated that the user could ask about again. Skip small talk. At most 12 facts per session; prefer specific details.

Each fact is a JSON object with exactly these fields:
- "subject": who or what it is about ("user" for the user)
- "slot": canonical key in the fixed form "entity.attribute": lower-case, singular nouns, underscores inside a part, the user is "user" (for example "user.job_title", "old_sneakers.storage_location", "sister.city", "user.marathon_personal_best"). Name the attribute that a newer value would replace, not the wording of this sentence. Use the same canonical words you would use for any session describing the same thing.
- "cardinality": "single" if a newer value replaces the older one (where something is stored, a current job, a current count, a current address); "multi" if values accumulate (purchases, interests, plans, events, recommendations)
- "predicate": short phrase; "object": the value, specific and complete, with numbers and units
- "value": a number if the fact is a countable quantity, else null; "unit": its unit or null
- "event_time": ISO date (YYYY-MM-DD) when the thing happened or is planned to happen, if the session says (resolve relative dates against the session date), else null
- "modality": "asserted", "decided", "proposed", "hypothetical" or "question"
- "scope": one of "personal", "work", "health", "travel", "finance", "hobby", "general"
- "author": "user" if the user stated it, "assistant" if only the assistant stated it
- "round": integer index of the round where it is stated
- "quote": a verbatim quote of at most 12 words supporting the fact
- "cue_keys": 3 to 6 short phrases a future question might use to ask about it, including synonyms and the category

Output exactly one JSON object: {{"sessions": [{{"session_id": "<id>", "facts": [ ... ]}}, ...]}} with one entry per session, in the given order.

{sessions}
"""

EXPAND_SYSTEM = "You output only valid JSON, no prose, no code fences."
EXPAND_TEMPLATE = """A user asks a memory system this question (current date {date}): {question}

Write 3 to 6 short phrases or words that a stored memory about the answer might be tagged with, including synonyms, the category of the thing asked about, and what the answer itself would be about (not words from the question only). Output exactly: {{"cue_keys": ["...", "..."]}}"""

READER_V12_RECIPE = "reader-v1.2"
READER_TEMPLATE_V12 = (
    "I will give you context drawn from past chat history between you and a user. Please answer the question based on "
    "the relevant information in it. First extract the relevant information, then reason over it (use the dates given "
    "and the current date when the question depends on time), then give the final answer on a last line starting "
    "with 'Answer:'. Items in the context are dated; when values conflict, prefer the most recent asserted or decided value, "
    "and treat a planned change as possibly completed if a later item implies it. If the context does not contain the "
    "information needed to answer, say that the information is not available instead of guessing.\n\n"
    "Context:\n\n{context}\n\nCurrent date: {date}\nQuestion: {question}\nAnswer (step by step):"
)

CANON_RECIPE = "canon-v1"
CANON_SYSTEM = "You output only valid JSON, no prose, no code fences."
CANON_TEMPLATE = """Below is a list of memory slot keys, each with its cardinality and the modalities it was stated with. Each key names an attribute of an entity, in the form entity.attribute. Different keys may name the SAME real-world attribute (for example a stored-at attribute and a plan to change where something is stored, or two spellings of the same attribute). You see only the keys; you do not see their content.

Group keys that name the same attribute of the same entity. Do not merge different attributes or different entities. Map every key to a canonical key, which must be one of the keys in the list (choose the clearest one); a key that is alone maps to itself.

Output exactly: {{"mapping": {{"<key>": "<canonical key>", ...}}}} containing every input key exactly once.

Keys:
{lines}
"""

AUDIT_RECIPE = "audit-v1"
AUDIT_SYSTEM = "You are a careful auditor. Output exactly one line per item in the form '<number>: yes' or '<number>: no' and nothing else."
AUDIT_TEMPLATE = """=== ITEM {i} ===
Slot: {slot}
Earlier statement (session date {d1}): "{q1}"
Later statement (session date {d2}): "{q2}"
Question: do both statements concern the same attribute, and does the later statement replace (or announce a replacement of) the earlier value? Answer yes or no.
=== END ITEM {i} ==="""
