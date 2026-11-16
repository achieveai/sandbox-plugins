#!/usr/bin/env python3
"""Deterministic sample data for the dashboard examples (reference/examples/*.json).

Writes three CSV files into the directory given as argv[1]:
  llm_usage.csv        one row per day x user x model (LLM usage across an org)
  service_metrics.csv  one row per 15 min x service x region (latency / errors)
  product_events.csv   one row per user event (funnel + retention)
"""
import csv
import math
import os
import random
import sys
from datetime import date, datetime, timedelta

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(OUT, exist_ok=True)
rng = random.Random(42)


def write(name, header, rows):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(name, len(rows), "rows")


# ---- 1. LLM usage -------------------------------------------------------------
TEAMS = {"Platform": 9, "Search": 7, "Growth": 6, "Support": 12, "Data Science": 8, "Legal": 4}
MODELS = {  # name: (input $/1M, output $/1M, latency ms base, share weight)
    "claude-sonnet": (3.0, 15.0, 2400, 0.34),
    "claude-haiku": (0.8, 4.0, 900, 0.22),
    "gpt-4.1": (2.0, 8.0, 2100, 0.2),
    "gpt-4.1-mini": (0.4, 1.6, 800, 0.14),
    "llama-70b": (0.6, 0.6, 1500, 0.1),
}
APPS = ["chat", "code-review", "support-bot", "search-rerank", "doc-summarizer"]
users = []
for team, n in TEAMS.items():
    for i in range(n):
        users.append((f"{team.split()[0].lower()}-{i + 1:02d}", team, rng.choice(APPS), rng.lognormvariate(0, 0.8)))
start = date(2026, 8, 17)
rows = []
for d in range(90):
    day = start + timedelta(days=d)
    weekday = day.weekday()
    growth = 1 + d / 120
    week_factor = 0.35 if weekday >= 5 else 1.0
    for user, team, app, intensity in users:
        if rng.random() > 0.8 * week_factor + 0.1:
            continue
        for model, (pin, pout, lat, share) in MODELS.items():
            # Support moves from claude-sonnet to claude-haiku in the last month.
            s = share
            if team == "Support" and d > 60:
                s = share * (0.3 if model == "claude-sonnet" else 2.2 if model == "claude-haiku" else 1)
            if rng.random() > s * 2:
                continue
            req = max(1, int(rng.lognormvariate(2.6, 0.7) * intensity * growth * week_factor))
            # A runaway agent loop in Data Science on two days.
            if team == "Data Science" and d in (71, 72) and model == "claude-sonnet":
                req *= 9
            tin = int(req * rng.gauss(2200 if app != "doc-summarizer" else 9000, 400))
            tout = int(req * rng.gauss(450, 90))
            cost = tin / 1e6 * pin + tout / 1e6 * pout
            errors = sum(1 for _ in range(min(req, 200)) if rng.random() < 0.012)
            latency = int(lat * rng.uniform(0.85, 1.25))
            rows.append([day.isoformat(), user, team, app, model, req, max(tin, 0), max(tout, 0),
                         round(cost, 4), errors, latency])
write("llm_usage.csv", ["date", "user", "team", "app", "model", "requests", "input_tokens", "output_tokens",
                        "cost_usd", "errors", "latency_ms"], rows)

# ---- 2. Service metrics -------------------------------------------------------
SERVICES = {"api-gateway": 45, "auth": 20, "search": 120, "checkout": 180, "inventory": 60, "notifications": 30}
REGIONS = {"us-east": 1.0, "eu-west": 0.6}
t0 = datetime(2026, 11, 8, 0, 0)
rows = []
for step in range(7 * 24 * 4):
    ts = t0 + timedelta(minutes=15 * step)
    hour = ts.hour + ts.minute / 60
    diurnal = 0.55 + 0.45 * math.sin((hour - 8) / 24 * 2 * math.pi) ** 2
    for svc, base_lat in SERVICES.items():
        for region, rfac in REGIONS.items():
            req = int(rng.gauss(4000, 300) * diurnal * rfac * (1.6 if svc == "api-gateway" else 1))
            err_rate = 0.002
            lat = base_lat
            # Incident: checkout in eu-west degraded for ~3 hours on day 5.
            if svc == "checkout" and region == "eu-west" and 4 * 24 * 4 + 54 <= step <= 4 * 24 * 4 + 66:
                err_rate, lat = 0.06, base_lat * 3.2
            # Slow creep in search latency over the week.
            if svc == "search":
                lat = base_lat * (1 + step / (7 * 24 * 4) * 0.5)
            errors = sum(1 for _ in range(200) if rng.random() < err_rate) * req // 200
            p50 = lat * rng.uniform(0.9, 1.1)
            rows.append([ts.strftime("%Y-%m-%dT%H:%M:00Z"), svc, region, req, errors,
                         round(p50, 1), round(p50 * rng.uniform(2.2, 2.8), 1), round(p50 * rng.uniform(4, 6), 1)])
write("service_metrics.csv", ["ts", "service", "region", "requests", "errors", "latency_p50_ms", "latency_p95_ms",
                              "latency_p99_ms"], rows)

# ---- 3. Product events --------------------------------------------------------
PLATFORMS = {"web": 0.55, "ios": 0.25, "android": 0.2}
COUNTRIES = ["US", "IN", "DE", "BR", "GB", "JP"]
CHANNELS = {"organic": 0.4, "paid-search": 0.25, "social": 0.2, "referral": 0.15}
rows = []
first = date(2026, 8, 3)
uid = 0
for wk in range(13):
    cohort_size = int(420 * (1 + wk * 0.05))
    for _ in range(cohort_size):
        uid += 1
        user = f"u{uid:05d}"
        platform = rng.choices(list(PLATFORMS), weights=PLATFORMS.values())[0]
        channel = rng.choices(list(CHANNELS), weights=CHANNELS.values())[0]
        country = rng.choice(COUNTRIES)
        d0 = first + timedelta(days=wk * 7 + rng.randrange(7))
        attrs = [platform, country, channel]
        rows.append([d0.isoformat(), user, "visit", *attrs])
        p_signup = 0.42 + (0.08 if channel == "referral" else 0) - (0.06 if platform == "android" else 0)
        if rng.random() > p_signup:
            continue
        rows.append([d0.isoformat(), user, "signup", *attrs])
        # Onboarding change in week 8 improves activation.
        p_act = 0.55 + (0.15 if wk >= 8 else 0)
        if rng.random() > p_act:
            continue
        rows.append([(d0 + timedelta(days=rng.randrange(3))).isoformat(), user, "activate", *attrs])
        if rng.random() < 0.28 + (0.07 if channel == "paid-search" else 0):
            rows.append([(d0 + timedelta(days=rng.randrange(2, 14))).isoformat(), user, "subscribe", *attrs])
        keep = 0.78 + (0.06 if wk >= 8 else 0)
        for w in range(1, 13):
            if rng.random() > keep:
                break
            day = d0 + timedelta(days=7 * w + rng.randrange(7))
            if day > date(2026, 11, 1):
                break
            rows.append([day.isoformat(), user, "active", *attrs])
rows.sort(key=lambda r: r[0])
write("product_events.csv", ["date", "user_id", "event", "platform", "country", "channel"], rows)
