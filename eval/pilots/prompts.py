"""Fixed prompts shared by the Stage 11 pilots. The judge system and the reader prompt are copied verbatim from the earlier lab
(phasef/prompts.py: JUDGE_SYSTEM, READER_SYSTEM, READER_TEMPLATE_V12 = recipe reader-v1.2-anon); the recipe strings are part of the cache key."""
JUDGE_RECIPE = "judge-v1"
READER_RECIPE = "reader-v1.2-anon"

JUDGE_SYSTEM = ("You are a careful grader. For each numbered item follow its instructions exactly. Output exactly one line per item, "
                "in the form '<number>: yes' or '<number>: no', and nothing else.")

READER_SYSTEM = "You answer questions about a user's past conversations using only the context provided. Be accurate and concise."

READER_TEMPLATE_V12 = (
    "I will give you context drawn from past chat history between you and a user. Please answer the question based on "
    "the relevant information in it. First extract the relevant information, then reason over it (use the dates given "
    "and the current date when the question depends on time), then give the final answer on a last line starting "
    "with 'Answer:'. Items in the context are dated; when values conflict, prefer the most recent asserted or decided value, "
    "and treat a planned change as possibly completed if a later item implies it. If the context does not contain the "
    "information needed to answer, say that the information is not available instead of guessing.\n\n"
    "Context:\n\n{context}\n\nCurrent date: {date}\nQuestion: {question}\nAnswer (step by step):"
)
