# app.py route for the product-funnel example: one row per user, derived from the raw events.
# Paste this in place of `ROUTES = {}` in app.py. The spec reads it as {"url": "api/users"}.
import csv

STEPS = ["visit", "signup", "activate", "subscribe"]


def users(query):
    by_user = {}
    with open(os.path.join(HERE, "data", "product_events.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            u = by_user.setdefault(r["user_id"], {"user_id": r["user_id"], "first_seen": r["date"], "channel": r["channel"],
                                                  "platform": r["platform"], "country": r["country"], "step": -1})
            u["first_seen"] = min(u["first_seen"], r["date"])
            if r["event"] in STEPS:
                u["step"] = max(u["step"], STEPS.index(r["event"]))
    out = []
    for u in by_user.values():
        if u["step"] < 0:
            continue  # active-only users with no funnel event
        u["furthest_step"] = ["Stopped at visit", "Signed up", "Activated", "Subscribed"][u.pop("step")]
        out.append(u)
    return out


ROUTES = {"/api/users": users}
