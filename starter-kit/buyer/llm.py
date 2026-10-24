"""Claude client with a hard spend cap. Every call's tokens are priced and added to runs/spend.json;
once the cap is reached, calls raise BudgetExceeded and the agent falls back to its template writer."""

import json
import os
import threading
import time
from pathlib import Path

import anthropic

PRICES = {  # USD per million tokens: input, output, cache read, cache write (5 min)
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
SPEND = Path(os.environ.get("BUYER_SPEND_FILE", "runs/spend.json"))
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


def spent():
    try:
        return json.loads(SPEND.read_text())["usd"]
    except (FileNotFoundError, ValueError, KeyError):
        return 0.0


def _record(model, usage):
    p_in, p_out, p_cr, p_cw = PRICES.get(model, PRICES["claude-opus-5-5"])
    usd = (
        (usage.input_tokens or 0) * p_in
        + (usage.output_tokens or 0) * p_out
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_cr
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p_cw
    ) / 1e6
    with _lock:
        SPEND.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(SPEND.read_text())
        except (FileNotFoundError, ValueError):
            state = {"usd": 0.0, "calls": 0}
        state["usd"] = round(state["usd"] + usd, 5)
        state["calls"] += 1
        SPEND.write_text(json.dumps(state))
    return usd


class Claude:
    """`complete(system, user) -> str` with a budget, a timeout and the refusal fallback."""

    def __init__(self, model=None, effort=None, budget=None, timeout=25.0):
        self.model = model or os.environ.get("LLM_MODEL", "claude-opus-5-5")
        self.effort = effort or os.environ.get("LLM_EFFORT", "low")
        self.budget = float(budget or os.environ.get("BUYER_BUDGET_USD", 14))
        # one try only: a slow e-mail falls back to the template instead of missing the 60-second limit
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=0)
        self.fallbacks = True

    def complete(self, system, user, max_tokens=4000, timeout=None):
        if spent() >= self.budget:
            raise BudgetExceeded(f"LLM budget of ${self.budget} used up")
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort},
        )
        t0 = time.monotonic()
        client = self.client.with_options(timeout=timeout) if timeout else self.client
        if self.fallbacks:
            try:
                r = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            except anthropic.BadRequestError:
                self.fallbacks = False  # this account or SDK does not take it: plain calls from now on
                r = client.messages.create(**kwargs)
        else:
            r = client.messages.create(**kwargs)
        usd = _record(r.model or self.model, r.usage)
        if r.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        self.last = {"seconds": round(time.monotonic() - t0, 1), "usd": round(usd, 4)}
        return "".join(b.text for b in r.content if b.type == "text")
