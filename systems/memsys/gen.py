"""Synthetic multi-tenant event log (no real data). Deterministic from the seed."""
import datetime as dt
import random

NAMES = ("Alice Bob Carol Dave Erin Frank Grace Heidi Ivan Judy Karl Liam Mona Nina Omar Pia Quin Rosa Sven Tara Uma Vik Wade "
         "Xena Yuri Zane Anya Boris Cleo Dmitri Elsa Farid Gita Hugo Iris Jonas Kira Leo Maya").split()
ATTRS = ["favourite colour", "favorite color", "city", "employer", "pet", "hobby", "car", "team", "birthday month", "editor"]
VALUES = {
    "favourite colour": "blue red green teal amber violet".split(), "favorite color": "blue red green teal amber violet".split(),
    "city": "Paris Oslo Lima Kyoto Cairo Quito Hanoi Perth".split(), "employer": "Acme Globex Initech Hooli Vandelay Umbra".split(),
    "pet": "cat dog parrot gecko rabbit".split(), "hobby": "chess climbing pottery sailing archery".split(),
    "car": "sedan coupe wagon pickup hatchback".split(), "team": "falcons otters wolves herons".split(),
    "birthday month": "January March May July September November".split(), "editor": "vim emacs nano helix".split()}
CHATTER = ["thanks that helps", "can you summarize that", "ok let us move on", "good morning", "I will think about it",
           "please remind me later", "that sounds right", "what else should we cover", "great work today"]
ANCHOR = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)


def gen_tenant(tenant, seed, n_sessions, per_session, n_users=6, n_entities=30, span_days=540):
    """Returns list of sessions: dict(session_id, subject_id, events=[dict])."""
    rng = random.Random(f"{seed}/{tenant}")
    ents = rng.sample(NAMES, n_entities)
    cur_val = {}
    hist = {}
    starts = sorted(rng.uniform(0, span_days * 86400) for _ in range(n_sessions))
    sessions = []
    for si, st in enumerate(starts):
        user = f"u{rng.randint(1, n_users)}"
        t = ANCHOR + dt.timedelta(seconds=st)
        evs = []
        for k in range(per_session):
            t += dt.timedelta(seconds=rng.randint(5, 300))
            r = rng.random()
            e = ents[min(int(rng.paretovariate(1.2)) - 1, n_entities - 1)] if rng.random() < 0.7 else rng.choice(ents)
            a = rng.choice(ATTRS)
            v = rng.choice(VALUES[a])
            actor, trust = "user", "user_typed"
            if r < 0.50:
                text = f"{e}'s {a} is {v}."
                cur_val[(e, a)] = v; hist.setdefault((e, a), []).append(v)
            elif r < 0.55:
                text = f"{e}'s phone for {rng.choice(['work', 'home'])} is 555-{rng.randint(1000, 9999)}."
            elif r < 0.59:
                text = f"If {e}'s {a} were {v}."
            elif r < 0.62:
                text = f"Proposal: set {e}'s {a} to {v}."
            elif r < 0.65:
                text = f"Decided: {e}'s {a} is {v}."
                cur_val[(e, a)] = v; hist.setdefault((e, a), []).append(v)
            elif r < 0.73:
                actor, trust = "assistant", "assistant"
                old = hist.get((e, a), [v])
                val = cur_val.get((e, a), v) if rng.random() < 0.7 else rng.choice(old)
                text = f"As you said, {e}'s {a} is {val}."
            elif r < 0.78:
                trust = "user_supplied_external"
                text = f"Pasted: {e}'s {a} is {v}."
            elif r < 0.82:
                actor, trust = "tool", "web"
                text = f"According to the page, {e}'s {a} is {v}."
            elif r < 0.825:
                text = f"{e} is now called {e}x."
            else:
                text = rng.choice(CHATTER) + f" {e}"
            evs.append(dict(ts_event=t, actor=actor, trust_origin=trust, kind="message", subject_id=user, content=text))
        sessions.append({"session_id": f"s{si:04d}", "subject_id": user, "events": evs})
    return sessions


def load(conn, tenant, sessions, seed, store, shuffle=True, chunk=100):
    rng = random.Random(f"{seed}/{tenant}/load")
    order = list(sessions)
    if shuffle:
        rng.shuffle(order)
    n = 0
    for s in order:
        for i in range(0, len(s["events"]), chunk):
            store.append_events(conn, tenant, s["session_id"], s["events"][i:i + chunk])
            n += len(s["events"][i:i + chunk])
    return n
