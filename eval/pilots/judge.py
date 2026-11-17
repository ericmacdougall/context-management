"""Judge prompts copied verbatim from the LongMemEval evaluation script.
Source: https://github.com/xiaowu0162/LongMemEval  src/evaluation/evaluate_qa.py
Last commit touching that file: d6dc8b50a2d9ac0c99485ea28fa5755c62414c34 (2024-11-24); file sha256 ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251.
Deviation (labelled): the original uses a one-line prompt and max_tokens=10; here each call carries ONE item wrapped as item 1 of the batch template
(JUDGE_SYSTEM, answer line "1: yes" or "1: no"), the same wrapper Stage G used. Judge model: claude-opus-5-5 (the original used gpt-4o).
LongMemEval is MIT licensed; see THIRD-PARTY-NOTICE-LONGMEMEVAL.txt in this directory.
"""
import re

from .prompts import JUDGE_SYSTEM


def get_anscheck_prompt(task, question, answer, response, abstention=False):
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        else:
            raise NotImplementedError(task)
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    return template.format(question, str(answer), response)


def build_batch_prompt(items):
    """items: list of (qtype, question, answer, response, abstention). Returns the batched judge prompt."""
    blocks = []
    for i, (task, q, a, r, abst) in enumerate(items, 1):
        blocks.append("=== ITEM %d ===\n%s\n=== END ITEM %d ===" % (i, get_anscheck_prompt(task, q, a, r, abst), i))
    return "\n\n".join(blocks) + "\n\nNow output one line per item: '<number>: yes' or '<number>: no'."


def parse_batch(text, n):
    labels = {}
    for m in re.finditer(r"^\s*(\d+)\s*[:.)-]\s*(yes|no)\b", text, re.I | re.M):
        labels.setdefault(int(m.group(1)), m.group(2).lower() == "yes")
    missing = [i for i in range(1, n + 1) if i not in labels]
    if missing:
        raise ValueError("judge output missing items %s: %r" % (missing, text[:300]))
    return [labels[i] for i in range(1, n + 1)]


def judge_prompt_one(task, question, answer, response, abstention=False):
    """The single-item judge prompt (batch of 1)."""
    return build_batch_prompt([(task, question, answer, response, abstention)])


def parse_one(text):
    return parse_batch(text, 1)[0]
