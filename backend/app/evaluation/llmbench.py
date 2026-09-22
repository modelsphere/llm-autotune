"""LLMBench adapter — routes all expensive benchmarking to the external
platform (poc-scope.md decision 4).

Contract verified against the LLMBench source (2026-07-30):

  auth     a personal API key ("llmb_…") is a bearer credential in its own
           right — no login round-trip. resolve_user_from_token() treats keys
           and JWTs identically, so a key confers the same permissions.
           Username/password login remains as a fallback.
  submit   POST /submissions/benchmarks/{slug}/submit  -> 202 {id, status, …}
           body: endpoint_url, model, api_key (of the *target* endpoint, ""
           for keyless), optional description_summary/_detail, optional
           hardware (cards_per_machine, machine_count, card_type), optional
           extra_params {concurrency_override | module_concurrency_overrides},
           optional contributor (a display name the submission is listed
           under), optional source_url (the run's page here, stored verbatim
           so a row there can link back to what produced it)
  poll     GET /submissions/{id} -> {status, score_total, runs[{module_name,
           metrics_json, …}], error}
           status: queued | running | done | failed | canceled
  cancel   POST /submissions/{id}/cancel

Two rejections are *not* run failures — the platform is momentarily unwilling,
not the config bad: 429 (per-user active-submission quota, default 8) and 503
(operator cordon). Both raise EvaluatorBusy so the supervisor retries later.

The submission id is the external_ref stored on our run row — after a worker
restart we resume polling by id instead of re-benchmarking.
"""

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from app.core.config import get_settings
from app.evaluation.base import (
    EvalOutcome,
    EvalStatus,
    Evaluator,
    EvaluatorBusy,
    EvaluatorRejected,
)

logger = logging.getLogger(__name__)

_TERMINAL_FAIL = {"failed", "canceled"}
API_KEY_PREFIX = "llmb_"

# Gateway hiccups worth waiting out inline: a proxy in front of LLMBench
# answering 502/504 while the service catches its breath. Retried in `_send`.
_RETRYABLE_STATUS = {502, 504}
# Every way the platform can be momentarily unwilling rather than the config
# bad. 429/503 are deliberate backpressure (quota, operator cordon); 502/504
# are a gateway that stayed down past our retries. All mean "defer and try
# again", never "fail the run".
_UNAVAILABLE_STATUS = {429, 502, 503, 504}


class LLMBenchClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.llmbench_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llmbench_api_key
        self.username = username if username is not None else settings.llmbench_username
        self.password = password if password is not None else settings.llmbench_password
        self._token: str | None = self.api_key or None
        # Injection points for tests: a MockTransport that fails N times, and a
        # sleep that does not actually wait.
        self._transport = transport
        self._sleep = sleep or time.sleep
        self._max_attempts = max(1, settings.llmbench_max_attempts)
        self._backoff = settings.llmbench_retry_backoff_seconds
        self._backoff_cap = settings.llmbench_retry_backoff_cap_seconds

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        """Only needed without an API key."""
        if self.api_key:
            self._token = self.api_key
            return
        response = self._send(
            "POST", "/auth/login", auth=False,
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]

    def _open(self, *, auth: bool) -> httpx.Client:
        headers: dict[str, str] = {}
        if auth:
            if self._token is None:
                self.login()
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.Client(
            base_url=self.base_url, headers=headers, timeout=60, transport=self._transport
        )

    def _retry_after_reauth(self, response: httpx.Response) -> bool:
        """A JWT can expire mid-campaign; an API key never does."""
        if response.status_code != 401 or self.api_key:
            return False
        self.login()
        return True

    def _send(self, method: str, path: str, *, auth: bool = True, **kwargs) -> httpx.Response:
        """One LLMBench call, retried through transient faults.

        A flaky intranet produces connect/read timeouts, resets and DNS
        failures, and a gateway in front of LLMBench answers 502/504 while it
        recovers. None of those is a verdict on the run, so ride them out with a
        bounded, generous backoff rather than let one bubble up and fail an
        otherwise-good config. Deliberate backpressure (429/503) and every other
        status are returned whole for the caller to interpret; a re-auth on an
        expired JWT is done once, out of band of the transient-retry budget.
        """
        last_error: httpx.TransportError | None = None
        for attempt in range(self._max_attempts):
            try:
                with self._open(auth=auth) as client:
                    response = client.request(method, path, **kwargs)
                if auth and self._retry_after_reauth(response):
                    with self._open(auth=auth) as client:
                        response = client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == self._max_attempts - 1:
                    raise
                self._wait(attempt, path, repr(exc))
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_attempts - 1:
                self._wait(attempt, path, f"HTTP {response.status_code}")
                continue
            return response
        raise last_error  # pragma: no cover - loop returns or raises above

    def _wait(self, attempt: int, path: str, reason: str) -> None:
        delay = min(self._backoff * (2**attempt), self._backoff_cap)
        logger.warning(
            "LLMBench %s transient failure (%s); retrying in %.0fs (attempt %d/%d)",
            path, reason, delay, attempt + 1, self._max_attempts,
        )
        self._sleep(delay)

    @staticmethod
    def _raise_if_busy(response: httpx.Response) -> None:
        if response.status_code in _UNAVAILABLE_STATUS:
            detail = response.text[:300]
            raise EvaluatorBusy(
                f"LLMBench temporarily unavailable ({response.status_code}): {detail}"
            )

    # -- API ----------------------------------------------------------------

    def submit(
        self,
        benchmark_slug: str,
        endpoint_url: str,
        model_name: str,
        endpoint_api_key: str = "",
        description_summary: str | None = None,
        description_detail: str | None = None,
        hardware: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
        contributor: str | None = None,
        source_url: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "endpoint_url": endpoint_url,
            "model": model_name,
            "api_key": endpoint_api_key,
        }
        # A display name: the benchmark platform lists every submission under
        # it, so it is worth sending whenever we have one.
        if contributor:
            body["contributor"] = contributor
        # Where this submission came from, so a row on the benchmark platform's
        # own list can link back to the run that produced it. Sent only when we
        # have one, so a platform that cannot address itself sends nothing
        # rather than something unfollowable.
        if source_url:
            body["source_url"] = source_url[:500]
        if description_summary:
            body["description_summary"] = description_summary[:100]
        if description_detail:
            body["description_detail"] = description_detail[:5000]
        if extra_params:
            body["extra_params"] = extra_params
        if hardware:
            body.update({k: v for k, v in hardware.items() if v is not None})

        try:
            response = self._send(
                "POST", f"/submissions/benchmarks/{benchmark_slug}/submit", json=body
            )
        except httpx.TransportError as exc:
            # Could not reach LLMBench at all after generous retries — the
            # platform is momentarily unavailable, not the config bad. Defer
            # (the supervisor keeps the run in HEALTH_CHECK) rather than fail it.
            raise EvaluatorBusy(f"LLMBench unreachable after retries: {exc}") from exc
        self._raise_if_busy(response)
        response.raise_for_status()  # 202 on success
        return str(response.json()["id"])

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One authenticated call, retried through transient faults and
        re-authenticated once if the token expired.

        The response is handed back whole, status and all: callers on other
        LLMBench resources care about *which* refusal they got (a 409 from the
        dataset builder is not an error), and raise_for_status flattens them.
        """
        return self._send(method, path, **kwargs)

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        response = self._send("GET", f"/submissions/{submission_id}")
        response.raise_for_status()
        return response.json()

    def modules_by_benchmark(self) -> dict[str, list[str]]:
        """Every benchmark this account can submit to, and what each one runs.

        Used to answer two questions before a night is committed to them: does
        this slug exist, and does the objective name metrics the benchmark's
        modules could possibly report. Both are typos, and both otherwise
        surface as a completed run that scored nothing.

        Module names are the instance keys the harvest will namespace metrics
        under (see `instance_keys`): a benchmark that runs one module twice
        lists `perf_guidellm_sweep` and `perf_guidellm_sweep#2`, so an
        objective on the second run validates the same way it is reported.
        """
        response = self._send("GET", "/benchmarks")
        response.raise_for_status()
        payload = response.json()
        benchmarks = payload.get("benchmarks", []) if isinstance(payload, dict) else payload
        return {
            b["slug"]: instance_keys(
                m.get("module_name", "") for m in (b.get("modules") or [])
            )
            for b in benchmarks
            if isinstance(b, dict) and b.get("slug")
        }

    def dataset_profiles_by_benchmark(self) -> dict[str, str]:
        """Which collection profile each benchmark's replay module resolves.

        Empty string where a benchmark has no replay module, or has one that
        does not resolve a profile (`dataset_source` other than "auto"). This
        is what makes pinning checkable: a campaign can pin all it likes, but
        if the benchmark it submits to resolves a different profile, every run
        replays something else and every result mismatches.
        """
        response = self._send("GET", "/benchmarks")
        response.raise_for_status()
        payload = response.json()
        benchmarks = payload.get("benchmarks", []) if isinstance(payload, dict) else payload
        wired: dict[str, str] = {}
        for benchmark in benchmarks or []:
            if not isinstance(benchmark, dict) or not benchmark.get("slug"):
                continue
            profile = ""
            for module in benchmark.get("modules") or []:
                params = module.get("params_json") or {}
                if params.get("dataset_profile"):
                    profile = str(params["dataset_profile"])
                    break
            wired[benchmark["slug"]] = profile
        return wired

    def cancel(self, submission_id: str) -> None:
        try:
            self._send("POST", f"/submissions/{submission_id}/cancel")
        except httpx.HTTPError as exc:
            logger.warning("LLMBench cancel(%s) failed: %s", submission_id, exc)

    def preflight(self, endpoint_url: str, model_name: str, api_key: str = "") -> dict[str, Any]:
        """LLMBench's own pre-submit endpoint probe.

        NOTE: their endpoint is session-gated (browser JWT only) — an API-key
        caller gets 403 by design. Usable only under username/password auth.
        """
        response = self._send(
            "POST", "/submissions/preflight",
            json={"endpoint_url": endpoint_url, "model": model_name, "api_key": api_key},
        )
        response.raise_for_status()
        return response.json()


def _failed_modules(submission: dict[str, Any]) -> str:
    """Name the modules that actually failed, and why, so the verdict is
    actionable rather than a bare "did not pass"."""
    parts: list[str] = []
    for name, module_run in _keyed_runs(submission):
        if module_run.get("passed") is False or module_run.get("status") == "failed":
            detail = _describe_module_failure(module_run)
            parts.append(f"{name}{f' ({detail})' if detail else ''}")
    return "; ".join(parts)


def _describe_module_failure(module_run: dict[str, Any]) -> str:
    """Why one module did not pass, in the most specific terms available.

    An explicit error is the platform's own words and wins. Otherwise the
    breached redlines are the reason `passed` is False — recover them so the
    verdict reads "uptime 0.06 < 0.99" rather than nothing. Failing both, the
    raw score is at least something to go on.
    """
    text = _first_line(module_run.get("error"), 200)
    if text:
        return text
    breaches = _redline_breaches(module_run)
    if breaches:
        shown = breaches[:_MAX_BREACHES_SHOWN]
        more = len(breaches) - len(shown)
        return "; ".join(shown) + (f"; +{more} more" if more else "")
    if module_run.get("score") is not None:
        return f"score {module_run['score']}"
    return ""


# A broadly-unhealthy run breaches many redlines at once — a benchmark can
# define a per-input-length TTFT ceiling for every bucket. Spell out the first
# few and count the rest, so the verdict stays a sentence, not a wall.
_MAX_BREACHES_SHOWN = 6


def _redline_breaches(module_run: dict[str, Any]) -> list[str]:
    """The redlines LLMBench failed this module on, as "metric actual op limit".

    LLMBench reports `passed: False` with no text saying which threshold broke:
    the verdict is implicit in metric_configs_json — entries with role
    "redline" carrying a min_val/max_val bound — checked against the values in
    metrics_json. Only a `redline` gates the run; a `display` bound (an overall
    ttft_p99 ceiling, say) is shown on the platform but does not fail it, so
    reporting it here would name a reason that was not one. A redline whose
    metric came back absent (a judge rate on a run that never reached judging)
    is skipped rather than guessed at.
    """
    metrics = module_run.get("metrics_json") or {}
    breaches: list[str] = []
    for cfg in module_run.get("metric_configs_json") or []:
        if cfg.get("role") != "redline":
            continue
        actual = _as_number(metrics.get(cfg.get("key")))
        if actual is None:
            continue
        low, high = cfg.get("min_val"), cfg.get("max_val")
        if low is not None and actual < low:
            breaches.append(f"{cfg['key']} {_fmt_num(actual)} < {_fmt_num(low)}")
        elif high is not None and actual > high:
            breaches.append(f"{cfg['key']} {_fmt_num(actual)} > {_fmt_num(high)}")
    return breaches


def module_reports(submission: dict[str, Any]) -> list[dict[str, Any]]:
    """Each module's verdict and thresholds, lifted whole from the submission
    payload — the redlines live on the BENCHMARK, not on anything we declare,
    so this is the only honest source for "which limits applied and did they
    hold". One entry per module run:

        {module, status, passed, score, error,
         bounds: [{metric, role, min, max, actual, ok}]}

    `role` is LLMBench's own: "redline" gates the run, "display" is a shown
    threshold that does not. `ok` is None when the metric never came back —
    unmeasured, not certified either way. Tolerates payloads with no
    metric_configs_json (the mock, older benchmarks): the module row still
    lands, with no bounds.

    `module` is the run's instance key — the prefix its metrics are filed
    under (`instance_keys`) — and `module_name` the LLMBench module it ran.
    `params` are that run's locked params (input_tokens, output_tokens, the
    concurrency grid…): the only thing telling two runs of one module apart,
    and the frozen record must say what was measured because the benchmark
    they came from can be edited afterwards.
    """
    reports: list[dict[str, Any]] = []
    for key, module_run in _keyed_runs(submission):
        metrics = module_run.get("metrics_json") or {}
        bounds: list[dict[str, Any]] = []
        for cfg in module_run.get("metric_configs_json") or []:
            role, low, high = cfg.get("role"), cfg.get("min_val"), cfg.get("max_val")
            if role not in ("redline", "display") or (low is None and high is None):
                continue
            actual = _as_number(metrics.get(cfg.get("key")))
            ok = None
            if actual is not None:
                ok = (low is None or actual >= low) and (high is None or actual <= high)
            bounds.append(
                {"metric": cfg.get("key"), "role": role, "min": low, "max": high,
                 "actual": actual, "ok": ok}
            )
        reports.append(
            {
                "module": key,
                "module_name": module_run.get("module_name", "module"),
                "params": _frozen_params(module_run.get("params_json")),
                "status": module_run.get("status", ""),
                "passed": module_run.get("passed"),
                "score": _as_number(module_run.get("score")),
                "error": _first_line(module_run.get("error"), 200),
                "bounds": bounds,
            }
        )
    return reports


def _frozen_params(params: Any) -> dict[str, Any]:
    """A run's locked params as LLMBench reported them, scalars only.

    LLMBench redacts credentials out of params_json before it reaches us, so
    what remains is the run's configuration and safe to freeze. Nulls are the
    unset defaults and say nothing; nested values (none today) are dropped
    rather than frozen unread.
    """
    if not isinstance(params, dict):
        return {}
    return {
        str(key): value
        for key, value in params.items()
        if value is not None and isinstance(value, str | int | float | bool)
    }


def _fmt_num(value: float) -> str:
    """A threshold number short enough to sit in a one-line verdict."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4g}"


def _first_line(text: str | None, limit: int) -> str:
    """First line of a message, tolerating None/empty/whitespace-only input.

    `"".splitlines()` is [] — indexing it raised IndexError and turned a
    correctly-detected benchmark failure into an unexplained supervisor crash.
    """
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


# LLMBench lets one benchmark run the same module more than once with
# different params — sweep-test runs perf_guidellm_sweep at 50k+1.5k AND at
# 40k+300 — and reports every run under the module's name. Namespacing by that
# name alone let the second run silently overwrite the first's every metric.
_INSTANCE_SEP = "#"


def instance_keys(module_names: Iterable[str]) -> list[str]:
    """One key per module run, distinct even when runs share a module.

    The first run of a module keeps the bare name — every existing objective
    (`perf_guidellm_sweep.output_tpm_card_norm`) and the metrics catalog
    address it unchanged — and each further run of the same module takes an
    ordinal suffix: `perf_guidellm_sweep#2`. Position is the only identity a
    submission payload offers (its runs carry no benchmark_module_id), and
    LLMBench lists runs in the benchmark's module order, so the suffix is
    stable across every submission of one benchmark. `#` because a metric key
    is split on `.` to find its module, and the suffix must never read as a
    real module name.
    """
    seen: dict[str, int] = {}
    keys: list[str] = []
    for name in module_names:
        nth = seen.get(name, 0) + 1
        seen[name] = nth
        keys.append(name if nth == 1 else f"{name}{_INSTANCE_SEP}{nth}")
    return keys


def _keyed_runs(submission: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    runs = submission.get("runs", [])
    keys = instance_keys(run.get("module_name", "module") for run in runs)
    return list(zip(keys, runs, strict=True))


# How deep to flatten nested metric groups. guidellm reports one group per
# concurrency level ("c1", "c2", "c4"), each a dict of the full metric set at
# that level; two is enough for `<module>.<level>.<metric>`. Bounded so a
# benchmark that returns something deeply nested cannot produce thousands of
# keys, and so the flattening cannot recurse without end.
_MAX_METRIC_DEPTH = 2


def _flatten_into(
    target: dict[str, Any], prefix: str, value: Any, depth: int = 0
) -> None:
    """Namespace one metric, descending into nested groups.

    Written generically rather than against known key names: the benchmark
    platform is external and its modules change. A new test with different
    fields must land as data, not as a code change here.
    """
    if isinstance(value, dict) and depth < _MAX_METRIC_DEPTH:
        for key, nested in value.items():
            _flatten_into(target, f"{prefix}.{key}", nested, depth + 1)
        return
    if isinstance(value, dict | list):
        # A group deeper than we flatten, or a series. Not a metric anyone can
        # write a redline against; `results.raw` keeps the original anyway.
        return
    target[prefix] = value


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _card_norm_scale(raw: dict[str, Any]) -> float | None:
    """The factor this module used to express its rates per card.

    LLMBench publishes several metrics twice — `output_tpm` alongside
    `output_tpm_card_norm` — where the second is the first rescaled as if the
    endpoint ran on a fixed number of cards. Recovering the factor from a pair
    it actually returned, rather than dividing by the constant 8, keeps this
    correct on the day that baseline changes, and yields nothing at all on a
    module that reports no normalized metrics (which is the honest answer).

    Sorted so the factor is derived from the same pair every time: two metrics
    could in principle disagree, and a number that silently depends on dict
    ordering is not one to rank a night's work by.
    """
    for key in sorted(raw):
        base, norm = _as_number(raw.get(key)), _as_number(raw.get(f"{key}_card_norm"))
        if base and norm is not None:
            return norm / base
    return None


def _module_score(module_run: dict[str, Any]) -> dict[str, Any]:
    """A module's own score, and the same score per card.

    The benchmark's score is a weighted sum of absolute token rates, so it
    rewards a config for occupying more GPUs — eight cards beat four at nearly
    anything. Dividing by the cards the config held asks the question worth
    answering, and because the divisor is LLMBench's own normalization factor
    the result is exactly what its weights would give applied to its own
    per-card metrics. Derived here rather than configured on the benchmark
    because there is no per-card counterpart for every term of the sum.
    """
    score = _as_number(module_run.get("score"))
    if score is None:
        return {}
    out: dict[str, Any] = {"score": module_run["score"]}
    scale = _card_norm_scale(module_run.get("metrics_json") or {})
    if scale is not None:
        out["score_card_norm"] = score * scale
    return out


def _flatten_metrics(submission: dict[str, Any]) -> dict[str, Any]:
    """Flatten per-module metrics_json into one namespaced dict of scalars.

    Nested groups are flattened too, which is what makes per-concurrency
    numbers addressable: guidellm's `c1` group is the single-stream case, so
    `perf_guidellm_sweep.c1.request_output_tps` is a minimum-single-stream-OTPS
    redline. Left nested, that data was present in the database and impossible
    to write an objective against.
    """
    metrics: dict[str, Any] = {}
    if submission.get("score_total") is not None:
        metrics["score_total"] = submission["score_total"]
    for module, module_run in _keyed_runs(submission):
        for suffix, value in _module_score(module_run).items():
            metrics[f"{module}.{suffix}"] = value
        for key, value in (module_run.get("metrics_json") or {}).items():
            _flatten_into(metrics, f"{module}.{key}", value)
    return metrics


class LLMBenchEvaluator(Evaluator):
    name = "llmbench"

    def __init__(self, client: LLMBenchClient | None = None, benchmark_slug: str | None = None):
        settings = get_settings()
        self.client = client or LLMBenchClient()
        self.benchmark_slug = benchmark_slug or settings.llmbench_benchmark_slug

    def start(self, endpoint_url: str, served_model_name: str, context: dict[str, Any]) -> str:
        """context: campaign/run metadata — config, hardware, overrides.

        Runs LLMBench's own preflight first (near-free, and it checks
        connectivity *from the benchmark platform's network position*, which
        our in-platform health check cannot).
        """
        endpoint_api_key = context.get("endpoint_api_key", "")
        if get_settings().llmbench_preflight:
            self._preflight_or_reject(endpoint_url, served_model_name, endpoint_api_key)
        return self.client.submit(
            benchmark_slug=context.get("benchmark_slug") or self.benchmark_slug,
            endpoint_url=endpoint_url,
            model_name=served_model_name,
            endpoint_api_key=endpoint_api_key,
            description_summary=context.get("description_summary"),
            description_detail=context.get("description_detail"),
            hardware=context.get("hardware"),
            extra_params=context.get("extra_params"),
            contributor=context.get("contributor"),
            source_url=context.get("source_url"),
        )

    def _preflight_or_reject(self, endpoint_url: str, model_name: str, api_key: str) -> None:
        if self.client.api_key:
            # LLMBench gates /submissions/preflight to browser sessions on
            # purpose ("NOT part of the programmatic API … can't be used as an
            # open SSRF probe"), so an API-key caller always gets 403. Our own
            # health gate covers the same ground; don't bother asking.
            return
        try:
            result = self.client.preflight(endpoint_url, model_name, api_key)
        except (httpx.HTTPError, KeyError) as exc:
            # Preflight itself is unavailable — don't block the benchmark on it.
            logger.warning("LLMBench preflight unavailable (%s); submitting anyway", exc)
            return
        if result.get("ok"):
            return
        failed = [
            f"{check.get('name')}: {check.get('detail')}"
            for check in result.get("checks", [])
            if check.get("status") == "fail"
        ]
        raise EvaluatorRejected(
            "LLMBench preflight failed — " + ("; ".join(failed) or str(result)[:300])
        )

    def poll(self, external_ref: str) -> EvalOutcome:
        try:
            submission = self.client.get_submission(external_ref)
        except httpx.HTTPError as exc:
            # Transient poll failure is not a run failure — stay RUNNING.
            logger.warning("LLMBench poll(%s) failed: %s", external_ref, exc)
            return EvalOutcome(status=EvalStatus.RUNNING, error=str(exc))

        status = submission.get("status", "")
        if status == "done":
            # `done` only means the modules RAN. LLMBench reports the verdict
            # separately in `passed`: False when a module breached a redline or
            # threshold, or was skipped because an earlier one did. Treating
            # `done` as success would let a service that fails its own
            # acceptance criteria pass the canary — or worse, let a config that
            # serves garbage at great throughput win the leaderboard.
            if submission.get("passed") is False:
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error="benchmark ran but did not pass: "
                    + (_failed_modules(submission) or "see submission detail"),
                    metrics=_flatten_metrics(submission),
                    raw=submission,
                )
            return EvalOutcome(
                status=EvalStatus.PASSED,
                metrics=_flatten_metrics(submission),
                raw=submission,
            )
        if status in _TERMINAL_FAIL:
            return EvalOutcome(
                status=EvalStatus.FAILED,
                error=submission.get("error") or f"LLMBench submission {status}",
                metrics=_flatten_metrics(submission),
                raw=submission,
            )
        return EvalOutcome(status=EvalStatus.RUNNING)

    def cancel(self, external_ref: str) -> None:
        self.client.cancel(external_ref)
