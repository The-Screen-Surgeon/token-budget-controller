"""Strict read-only Codex app-server usage adapter.

Only rate-limit percentages are exposed by the current app-server surface; this
adapter never reads or records conversation content or invokes a model.
"""
from __future__ import annotations

import json
import subprocess
import time
import queue
import threading
from typing import Any, Callable, Mapping

SCALE = 1_000_000


class CodexUsageError(RuntimeError):
    pass


class CodexAppServerAdapter:
    name = "codex-app-server"

    def __init__(self, binary: str = "codex", *, timeout: float = 8.0,
                 popen: Callable[..., Any] = subprocess.Popen,
                 clock: Callable[[], float] = time.time):
        if timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        self.binary, self.timeout, self._popen, self._clock = binary, timeout, popen, clock

    def _request(self, proc: Any, method: str, params: Mapping[str, Any] | None, ident: int) -> Any:
        message = {"method": method, "id": ident}
        if params is not None: message["params"] = params
        line = json.dumps(message, separators=(",", ":"))
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = max(0.001, deadline - time.monotonic())
            result: queue.Queue = queue.Queue(maxsize=1)
            threading.Thread(target=lambda: result.put(proc.stdout.readline()), daemon=True).start()
            try: raw = result.get(timeout=remaining)
            except queue.Empty: raise TimeoutError("Codex app-server request timed out") from None
            if not raw:
                raise CodexUsageError("Codex app-server closed the protocol stream")
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                raise CodexUsageError("malformed Codex app-server response") from None
            if not isinstance(msg, dict):
                raise CodexUsageError("malformed Codex app-server response")
            if msg.get("id") != ident:
                continue
            if "error" in msg or "result" not in msg:
                raise CodexUsageError("Codex app-server request failed")
            return msg["result"]
        raise TimeoutError("Codex app-server request timed out")

    def _notify(self, proc: Any, method: str, params: Mapping[str, Any]) -> None:
        proc.stdin.write(json.dumps({"method": method, "params": params}, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    @staticmethod
    def _strict_window(obj: Any, label: str, now: int) -> dict[str, Any]:
        if not isinstance(obj, dict):
            raise CodexUsageError(f"missing {label} rate limit")
        # Codex app-server protocol v2 RateLimitWindow.
        if set(obj) != {"usedPercent", "windowDurationMins", "resetsAt"}:
            raise CodexUsageError(f"unsupported {label} rate limit shape")
        pct, duration, reset = obj["usedPercent"], obj["windowDurationMins"], obj["resetsAt"]
        if type(pct) is not int or not 0 <= pct <= 100:
            raise CodexUsageError(f"invalid {label} usedPercent")
        if type(duration) is not int or duration <= 0 or type(reset) is not int or reset <= now:
            raise CodexUsageError(f"invalid {label} reset metadata")
        micros = pct * SCALE
        # Reset timestamp is a factual segment identity and expiration.
        return {"used_micropct": micros, "used_percent": pct,
                "window_duration_mins": duration, "reset_id": str(reset),
                "resets_at": reset}

    @classmethod
    def validate_rate_limits(cls, result: Any, *, now: int) -> dict[str, Any]:
        response_fields = {"ordinaryUsageAllowed", "rateLimits", "rateLimitsByLimitId",
                           "rateLimitResetCredits", "accountId", "rateLimitUpsell"}
        if not isinstance(result, dict) or not ("rateLimits" in result or "rateLimitsByLimitId" in result) or set(result) - response_fields:
            raise CodexUsageError("malformed account/rateLimits/read result")
        if "ordinaryUsageAllowed" in result and result["ordinaryUsageAllowed"] is not None and type(result["ordinaryUsageAllowed"]) is not bool:
            raise CodexUsageError("invalid ordinaryUsageAllowed")
        if "accountId" in result and result["accountId"] is not None and not isinstance(result["accountId"], str):
            raise CodexUsageError("invalid account identifier")
        by_id = result.get("rateLimitsByLimitId")
        if by_id is not None:
            if not isinstance(by_id, dict) or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in by_id.items()):
                raise CodexUsageError("invalid rateLimitsByLimitId")
        if isinstance(by_id, dict) and by_id:
            rate = by_id.get("codex")
            if not isinstance(rate, dict):
                raise CodexUsageError("Codex rate limit bucket is missing or ambiguous")
        else:
            rate = result.get("rateLimits")
            if not isinstance(rate, dict): raise CodexUsageError("single Codex rate limit bucket is missing")
        rate_fields = {"limitId", "limitName", "normalModelSlug", "primary", "secondary",
                       "credits", "individualLimit", "spendControlReached", "planType",
                       "rateLimitReachedType"}
        if not isinstance(rate, dict) or not {"primary", "secondary"}.issubset(rate) or set(rate) - rate_fields:
            raise CodexUsageError("missing primary or secondary rate limit")
        for key in ("limitId", "limitName", "normalModelSlug", "planType", "rateLimitReachedType"):
            if key in rate and rate[key] is not None and not isinstance(rate[key], str):
                raise CodexUsageError(f"invalid rate limit {key}")
        if "spendControlReached" in rate and rate["spendControlReached"] is not None and type(rate["spendControlReached"]) is not bool:
            raise CodexUsageError("invalid spendControlReached")
        credits = rate.get("credits")
        if credits is not None and (not isinstance(credits, dict) or set(credits) != {"hasCredits", "unlimited", "balance"}
                or type(credits["hasCredits"]) is not bool or type(credits["unlimited"]) is not bool
                or (credits["balance"] is not None and not isinstance(credits["balance"], str))):
            raise CodexUsageError("invalid credits metadata")
        individual = rate.get("individualLimit")
        if individual is not None and (not isinstance(individual, dict)
                or set(individual) != {"limit", "used", "remainingPercent", "resetsAt"}
                or not isinstance(individual["limit"], str) or not isinstance(individual["used"], str)
                or type(individual["remainingPercent"]) is not int or type(individual["resetsAt"]) is not int):
            raise CodexUsageError("invalid individual limit metadata")
        reset_credits = result.get("rateLimitResetCredits")
        if reset_credits is not None and (not isinstance(reset_credits, dict)
                or set(reset_credits) != {"availableCount", "credits"}
                or type(reset_credits["availableCount"]) is not int or reset_credits["availableCount"] < 0
                or (reset_credits["credits"] is not None and not isinstance(reset_credits["credits"], list))):
            raise CodexUsageError("invalid reset credits metadata")
        # This optional server-owned banner is not part of quota facts; reject
        # non-null values instead of carrying unstructured text through the adapter.
        if result.get("rateLimitUpsell") is not None:
            raise CodexUsageError("unsupported rate limit upsell payload")
        if by_id is not None:
            for snapshot in by_id.values():
                if not isinstance(snapshot, dict) or set(snapshot) - rate_fields:
                    raise CodexUsageError("invalid rate limit bucket")
                for win_name in ("primary", "secondary"):
                    if snapshot.get(win_name) is not None:
                        cls._strict_window(snapshot[win_name], win_name, now)
        return {key: cls._strict_window(rate[key], key, now) for key in ("primary", "secondary")}

    @staticmethod
    def validate_account_usage(usage: Any) -> None:
        if not isinstance(usage, dict) or set(usage) - {"summary", "dailyUsageBuckets", "threadUsage"} or not isinstance(usage.get("summary"), dict):
            raise CodexUsageError("malformed account/usage/read result")
        summary = usage["summary"]
        expected = {"lifetimeTokens", "peakDailyTokens", "longestRunningTurnSec",
                    "currentStreakDays", "longestStreakDays"}
        if set(summary) != expected:
            raise CodexUsageError("unsupported account usage summary shape")
        for value in summary.values():
            if value is not None and (type(value) is not int or value < 0):
                raise CodexUsageError("invalid account usage summary value")
        buckets = usage.get("dailyUsageBuckets")
        if buckets is not None:
            if not isinstance(buckets, list): raise CodexUsageError("invalid daily usage buckets")
            for bucket in buckets:
                if not isinstance(bucket, dict) or set(bucket) != {"startDate", "tokens"} or not isinstance(bucket["startDate"], str) or type(bucket["tokens"]) is not int or bucket["tokens"] < 0:
                    raise CodexUsageError("invalid daily usage bucket")
        if "threadUsage" in usage and usage["threadUsage"] is not None:
            raise CodexUsageError("unexpected thread usage in account-wide read")

    def snapshot(self) -> Mapping[str, Any]:
        proc = None
        observed = int(self._clock())
        try:
            proc = self._popen([self.binary, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, bufsize=1)
            initialized = self._request(proc, "initialize", {"clientInfo": {
                "name": "token_budget", "title": "Token Budget", "version": "1.0.0"}}, 1)
            if not isinstance(initialized, dict):
                raise CodexUsageError("malformed initialize response")
            self._notify(proc, "initialized", {})
            limits = self._request(proc, "account/rateLimits/read", None, 2)
            windows = self.validate_rate_limits(limits, now=observed)
            # Read account usage as required by the API contract; it is strictly
            # validated and discarded because it contains no per-call token facts.
            usage = self._request(proc, "account/usage/read", None, 3)
            self.validate_account_usage(usage)
            return {"provider": "codex", "observed_at": observed,
                    "windows": windows, "source": self.name,
                    "provenance": "account/rateLimits/read; percentages reported by Codex app-server",
                    "account_usage_read": True}
        except FileNotFoundError as exc:
            raise CodexUsageError("Codex CLI binary is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Codex app-server timed out") from exc
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    try:
                        proc.kill(); proc.wait(timeout=1)
                    except Exception:
                        pass
