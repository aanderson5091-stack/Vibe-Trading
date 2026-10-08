"""Resolves a decision client: TypeSafe direct, then the Vercel AI Gateway,
then a mock. Prints one banner line naming the winner. Every response is
logged with the model that actually answered, because confidence gates are
calibrated to one model and a silent upgrade breaks them quietly.

Resolution order (matches the article's setup notes and Lewis's gateway
verification on 2026-09-21):
  1. TYPESAFE_API_KEY  -> https://api.typesafe.ai/v1/systemone, model jev-latest
  2. AI_GATEWAY_API_KEY -> https://ai-gateway.vercel.sh/typesafe/v1/systemone,
     model typesafe-ai/jev (adds one network hop versus the direct API)
  3. Nothing found, or the gateway returns 403 customer_verification_required
     -> MockDecisionClient. The mock is never silent about being a mock.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from dataclasses import dataclass

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: uv pip install requests"
    ) from exc

TYPESAFE_DIRECT_URL = "https://api.typesafe.ai/v1/systemone"
GATEWAY_URL = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
GATEWAY_BILLING_URL = (
    "https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card"
)

# 429 is NOT retried inside a tick: retrying a rate limit only adds requests.
# It raises RateLimited and JevThrottle backs off across ticks instead.
RETRYABLE_STATUS = {529}
MAX_RETRIES = 2
BACKOFF_BASE_S = 0.35

# One shared lock for every Jev request in this process: never parallel calls.
_CALL_LOCK = threading.Lock()


class GatewayVerificationRequired(Exception):
    """Raised when the gateway responds 403 customer_verification_required."""


class DecisionClientError(Exception):
    pass


class RateLimited(DecisionClientError):
    """429, or any error whose text says the request rate is too high
    (the gateway's "access frequency is too high")."""


def _is_rate_limit(status: int, text: str) -> bool:
    t = (text or "").lower()
    return status == 429 or "frequency" in t or "rate limit" in t or "too many requests" in t


def _post_with_retry(url: str, headers: dict, body: dict, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    attempt = 0
    last_exc: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DecisionClientError(
                "block deadline exceeded before a request could be sent"
            )
        try:
            resp = requests.post(
                url, headers=headers, json=body, timeout=min(remaining, timeout)
            )
        except requests.RequestException as exc:
            last_exc = exc
            resp = None

        if resp is not None:
            if resp.status_code == 200:
                return resp.json()
            if _is_rate_limit(resp.status_code, resp.text):
                raise RateLimited(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 403:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                if (
                    payload.get("error", {}).get("type")
                    == "customer_verification_required"
                ):
                    raise GatewayVerificationRequired(
                        payload.get("error", {}).get(
                            "message", "customer verification required"
                        )
                    )
                raise DecisionClientError(f"HTTP 403: {resp.text[:300]}")
            if resp.status_code not in RETRYABLE_STATUS:
                raise DecisionClientError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        attempt += 1
        if attempt > MAX_RETRIES:
            if resp is not None:
                raise DecisionClientError(
                    f"gave up after {MAX_RETRIES} retries, last status {resp.status_code}"
                )
            raise DecisionClientError(
                f"gave up after {MAX_RETRIES} retries: {last_exc}"
            )

        backoff = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.1)
        remaining = deadline - time.monotonic()
        if backoff >= remaining:
            raise DecisionClientError("block deadline exceeded during backoff")
        time.sleep(backoff)


@dataclass
class BaseDecisionClient:
    name: str
    model: str

    def ask(self, state: dict, questions: dict, timeout: float) -> tuple[dict, dict]:
        raise NotImplementedError


class TypeSafeDirectClient(BaseDecisionClient):
    def __init__(self, api_key: str, model: str = "jev-latest"):
        super().__init__(name="TypeSafe direct", model=model)
        self._api_key = api_key

    def ask(self, state: dict, questions: dict, timeout: float) -> tuple[dict, dict]:
        t0 = time.monotonic()
        body = {"state": state, "model": self.model, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with _CALL_LOCK:
            data = _post_with_retry(TYPESAFE_DIRECT_URL, headers, body, timeout)
        latency_ms = (time.monotonic() - t0) * 1000
        meta = {
            "route": self.name,
            "model": data.get("model", self.model),
            "latency_ms": round(latency_ms, 1),
            "usage": data.get("usage", {}),
        }
        return data["answers"], meta


class GatewayClient(BaseDecisionClient):
    def __init__(self, api_key: str, model: str = "typesafe-ai/jev"):
        super().__init__(name="Vercel AI Gateway", model=model)
        self._api_key = api_key

    def ask(self, state: dict, questions: dict, timeout: float) -> tuple[dict, dict]:
        t0 = time.monotonic()
        body = {"state": state, "model": self.model, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with _CALL_LOCK:
            data = _post_with_retry(GATEWAY_URL, headers, body, timeout)
        latency_ms = (time.monotonic() - t0) * 1000
        meta = {
            "route": self.name,
            "model": data.get("model", self.model),
            "latency_ms": round(latency_ms, 1),
            "usage": data.get("usage", {}),
        }
        return data["answers"], meta


class MockDecisionClient(BaseDecisionClient):
    """A plausible, persistent, clearly-labelled stand-in.

    Answers are derived from the real snapshot fields (not pure noise) so a
    demo run still looks internally consistent, and a slow-moving latent
    factor keeps regime/direction from flickering every tick. It is never
    presented as real: the model string always starts with 'mock-'.
    """

    def __init__(self, seed: int | None = None):
        super().__init__(name="MOCK", model="mock-jev-0.1")
        self._rng = random.Random(seed)
        self._regime_latent = self._rng.uniform(-1, 1)
        self._direction_latent = self._rng.uniform(-1, 1)

    def _drift(self, latent: float, pull: float = 0.04) -> float:
        latent += self._rng.uniform(-0.16, 0.16) - pull * latent
        return max(-1.0, min(1.0, latent))

    def ask(self, state: dict, questions: dict, timeout: float) -> tuple[dict, dict]:
        t0 = time.monotonic()
        time.sleep(self._rng.uniform(0.04, 0.15))  # a mock still "costs" some latency

        self._regime_latent = self._drift(self._regime_latent)
        self._direction_latent = self._drift(self._direction_latent)

        imbalance = state.get("imbalance", 0.0) or 0.0
        toxic = max(
            0.0, min(1.0, 0.5 + imbalance * 0.6 + self._rng.uniform(-0.15, 0.15))
        )
        liquidity_stressed = max(0.0, min(1.0, 0.3 + self._rng.uniform(-0.2, 0.3)))

        regime_probs = _softmax_from_latent(
            self._regime_latent, ["trending", "mean_reverting", "high_vol", "crisis"]
        )
        direction_probs = _softmax_from_latent(
            self._direction_latent, ["up", "down", "neutral"]
        )

        env_score, env_probs = _score_from_latent(
            0.5 - liquidity_stressed + self._rng.uniform(-0.3, 0.3), 4
        )
        inv_pressure = abs(state.get("inventory", 0.0) or 0.0)
        pressure_score, pressure_probs = _score_from_latent(
            min(1.0, inv_pressure * 400) - 0.5, 4
        )

        fill_ratio = state.get("fill_ratio")
        fill_ratio = 1.0 if fill_ratio is None else fill_ratio
        reject_count = state.get("reject_count") or 0
        latencies = state.get("last_10_latencies_ms") or []
        avg_latency = sum(latencies) / len(latencies) if latencies else 100.0
        slippage = state.get("last_10_slippage_bps") or []
        avg_slippage = (
            sum(abs(s) for s in slippage) / len(slippage) if slippage else 0.0
        )
        health_latent = (
            (fill_ratio - 0.5) * 1.5
            - reject_count * 0.3
            - max(0.0, (avg_latency - 300) / 500)
            - avg_slippage / 20
            + self._rng.uniform(-0.2, 0.2)
        )
        health_score, health_probs = _score_from_latent(
            max(-1.0, min(1.0, health_latent)), 4
        )

        answers = {
            "regime": _choice_answer(regime_probs),
            "direction": _choice_answer(direction_probs),
            "toxic_flow": {"type": "noul", "noul": round(toxic, 4)},
            "liquidity_stressed": {
                "type": "noul",
                "noul": round(liquidity_stressed, 4),
            },
            "quote_environment": _score_answer(
                env_score,
                env_probs,
                ["Do not quote", "Marginal", "Standard", "Excellent"],
            ),
            "inventory_pressure": _score_answer(
                pressure_score,
                pressure_probs,
                ["None", "Mild", "Skew hard", "Reduce now"],
            ),
            "execution_health": _score_answer(
                health_score,
                health_probs,
                ["Broken", "Degraded", "Normal", "Optimal"],
            ),
        }
        latency_ms = (time.monotonic() - t0) * 1000
        meta = {
            "route": self.name,
            "model": self.model,
            "latency_ms": round(latency_ms, 1),
            "usage": {},
        }
        return answers, meta


def _softmax_from_latent(latent: float, options: list[str]) -> dict:
    import math

    scores = {
        opt: math.exp(4.6 * latent * (1 if i == 0 else -0.5))
        for i, opt in enumerate(options)
    }
    total = sum(scores.values())
    return {opt: v / total for opt, v in scores.items()}


def _choice_confidence(probs: dict) -> float:
    n = len(probs)
    peak = max(probs.values())
    if n <= 1:
        return 1.0
    return max(0.0, min(1.0, (n * peak - 1) / (n - 1)))


def _choice_answer(probs: dict) -> dict:
    choice = max(probs, key=probs.get)
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": {k: round(v, 4) for k, v in probs.items()},
        "confidence": round(_choice_confidence(probs), 4),
    }


def _score_from_latent(latent: float, n_levels: int) -> tuple[float, list[float]]:
    import math

    centre = max(0.0, min(n_levels - 1, (latent + 1) / 2 * (n_levels - 1)))
    weights = [math.exp(-((i - centre) ** 2) / 0.42) for i in range(n_levels)]
    total = sum(weights)
    probs = [w / total for w in weights]
    score = sum(i * p for i, p in enumerate(probs))
    return score, probs


def _score_answer(score: float, probs: list[float], levels: list[str]) -> dict:
    legend = {str(i): lvl for i, lvl in enumerate(levels)}
    prob_map = {str(i): round(p, 4) for i, p in enumerate(probs)}
    return {
        "type": "score",
        "score": round(score, 4),
        "legend": legend,
        "probabilities": prob_map,
        "confidence": round(
            _choice_confidence({str(i): p for i, p in enumerate(probs)}), 4
        ),
    }


class JevThrottle:
    """Decides when the loop may call Jev, and keeps the last good judgment.

    The fast loop runs every tick on price and risk. Jev is called only when
    JEV_MIN_INTERVAL_S has passed since the last call (default 4s), or when
    the state changed materially and JEV_MIN_GAP_S has passed (default 3s).
    A rate-limit error starts an exponential cooldown (5s doubling to 120s)
    during which the last judgment is reused. A judgment older than
    JEV_MAX_JUDGMENT_AGE_S is never reused. The Vercel AI Gateway allows
    roughly 20 to 30 requests a minute; a direct TypeSafe key allows more,
    so lower the intervals only with one.
    """

    def __init__(
        self,
        min_interval_s: float | None = None,
        min_gap_s: float | None = None,
        move_bps: float | None = None,
        max_age_s: float | None = None,
        backoff_base_s: float = 5.0,
        backoff_max_s: float = 120.0,
    ):
        env = os.environ.get
        self.min_interval_s = min_interval_s if min_interval_s is not None else float(env("JEV_MIN_INTERVAL_S") or 4.0)
        self.min_gap_s = min_gap_s if min_gap_s is not None else float(env("JEV_MIN_GAP_S") or 3.0)
        self.move_bps = move_bps if move_bps is not None else float(env("JEV_MATERIAL_MOVE_BPS") or 15.0)
        self.max_age_s = max_age_s if max_age_s is not None else float(env("JEV_MAX_JUDGMENT_AGE_S") or 60.0)
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.answers: dict | None = None
        self.meta: dict | None = None
        self.answered_at = -math.inf
        self.last_call_at = -math.inf
        self.cooldown_until = -math.inf
        self.strikes = 0
        self._ref: dict | None = None

    def _material_change(self, snap: dict) -> str | None:
        ref = self._ref
        if ref is None:
            return "first judgment"
        mid, ref_mid = snap.get("mid") or 0.0, ref.get("mid") or 0.0
        if ref_mid and abs(mid - ref_mid) / ref_mid * 10_000 >= self.move_bps:
            return "price moved"
        if abs((snap.get("spread_bps") or 0.0) - (ref.get("spread_bps") or 0.0)) >= self.move_bps:
            return "spread moved"
        inv, ref_inv = snap.get("inventory") or 0.0, ref.get("inventory") or 0.0
        if (inv > 0) != (ref_inv > 0) or (inv < 0) != (ref_inv < 0):
            return "position changed"
        return None

    def due(self, snap: dict, now: float) -> str | None:
        """Why Jev should be called this tick, or None to reuse the last judgment."""
        if now < self.cooldown_until:
            return None
        since = now - self.last_call_at
        if since >= self.min_interval_s:
            return "interval"
        if since >= self.min_gap_s:
            return self._material_change(snap)
        return None

    def called(self, now: float) -> None:
        self.last_call_at = now

    def success(self, answers: dict, meta: dict, snap: dict, now: float) -> None:
        self.answers, self.meta, self.answered_at = answers, meta, now
        self._ref = {k: snap.get(k) for k in ("mid", "spread_bps", "inventory")}
        self.strikes = 0

    def rate_limited(self, now: float) -> float:
        """Start (or extend) the exponential cooldown. Returns the wait in seconds."""
        self.strikes += 1
        wait = min(self.backoff_max_s, self.backoff_base_s * 2 ** (self.strikes - 1))
        wait += random.uniform(0, 0.2 * wait)
        self.cooldown_until = now + wait
        return wait

    def last(self, now: float) -> tuple[dict, dict] | None:
        """The last judgment if it is young enough to reuse, else None."""
        if self.answers is None or now - self.answered_at > self.max_age_s:
            return None
        age = now - self.answered_at
        return self.answers, dict(self.meta or {}, reused=True, age_s=round(age, 1))


def resolve_decision_client(mock: bool = False) -> BaseDecisionClient:
    """Pick a client and print the one-line banner. Never crashes: falls all
    the way through to the mock if nothing else is usable."""
    if mock:
        print(
            "MOCK DECISION CLIENT (forced by --mock): answers are plausible, persistent, and clearly not real."
        )
        return MockDecisionClient()

    typesafe_key = os.environ.get("TYPESAFE_API_KEY")
    gateway_key = os.environ.get("AI_GATEWAY_API_KEY")

    if typesafe_key:
        print(
            f"decision client: TypeSafe direct, model pinned to jev-latest ({TYPESAFE_DIRECT_URL})"
        )
        return TypeSafeDirectClient(typesafe_key)

    if gateway_key:
        print(
            f"decision client: Vercel AI Gateway, model typesafe-ai/jev ({GATEWAY_URL}, adds one network hop)"
        )
        return GatewayClient(gateway_key)

    print(
        "MOCK DECISION CLIENT: no TYPESAFE_API_KEY or AI_GATEWAY_API_KEY found. "
        "Answers are plausible, persistent, and clearly not real."
    )
    return MockDecisionClient()
