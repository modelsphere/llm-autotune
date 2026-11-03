"""Quality floors relative to production.

A finalist is held to production's quality score, the average of production's
measurements in the campaign, on the same fixed questions. A finalist measured
before production has nothing to be compared with and does not pass; once
production lands, it is judged again.
"""

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import quality
from app.control.recipe import Recipe
from app.db.base import Base
from app.db.models import Campaign, Candidate, Result, Run, User
from app.evaluation.benchmark_spec import BenchmarkSpec, QualitySpec, document
from app.measures import reader_for

QWEN = Path(__file__).parent / "data/catalog_variants/qwen3.6-35b-a3b__sglang-tp2-h100.yaml"
GPQA = "opencompass/accuracy[gpqa_diamond]"
OBJECTIVE = {
    "target_metric": "total_tpm_per_gpu",
    "redlines": quality.quality_floor(["gpqa_diamond"], 0.10),
}


# -- the benchmark ----------------------------------------------------------------


def test_a_quality_check_is_a_second_module_on_fixed_questions():
    plain = BenchmarkSpec(kind="replay", dataset_profile="p", requests=500, concurrency=8)
    checked = plain.model_copy(update={"quality": QualitySpec()})
    assert plain.slug != checked.slug
    # A spec without quality keeps the slug it always had.
    assert "quality" not in plain.essentials()

    modules = document(checked)["modules"]
    assert [m["module_name"] for m in modules] == ["replay", "opencompass"]
    params = modules[1]["params"]
    assert params["selected_benchmarks"] == ["gpqa_diamond"]
    assert params["sample_cap_gpqa"] == 100  # the first 100: the same every run
    assert params["avg_k_gpqa"] == 1 and params["pass_k_gpqa"] == 1  # once each, not avg@8
    assert params["force_temperature"] is False  # k = 1 runs greedy
    assert params["max_tokens"] == 16384 and params["max_workers"] == 16
    assert modules[1]["weight"] == 0.0


def test_unknown_quality_tasks_are_refused():
    with pytest.raises(ValueError, match="unknown quality tasks"):
        QualitySpec(tasks=["gpqa_diamond", "trivia"])


# -- the floor ----------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        recipe = Recipe.from_variant(yaml.safe_load(QWEN.read_text()))
        session.add(Campaign(
            id=1, owner_id=1, name="c", engine="sglang", image="img", model_path="/m",
            served_model_name="m", search_space={}, recipe=recipe.model_dump(),
            objective=OBJECTIVE, verify_objective=OBJECTIVE,
        ))
        session.commit()
        yield session


def _measure(db, config: dict, gpqa: float, *, baseline: bool = False) -> Result:
    campaign = db.get(Campaign, 1)
    n = db.query(Candidate).count() + 1
    db.add(Candidate(id=n, campaign_id=1, config=config, config_hash=f"h{n}",
                     is_baseline=baseline))
    db.add(Run(id=n, campaign_id=1, candidate_id=n))
    metrics = {"replay.input_tpm": 700_000.0, "replay.output_tpm": 20_000.0,
               "opencompass.gpqa_diamond": gpqa}
    reader = reader_for(metrics, {}, config, campaign)
    stored = quality.judge(db, campaign, OBJECTIVE, reader).as_dict()
    result = Result(run_id=n, source="llmbench", metrics=metrics, **stored)
    db.add(result)
    db.flush()
    return result


def test_the_floor_is_production_s_average_less_the_allowed_drop(db):
    campaign = db.get(Campaign, 1)
    early = _measure(db, {"kv_cache_dtype": "fp8_e4m3"}, 62.0)
    # Production not measured yet: nothing to compare with, so not a winner.
    assert not early.feasible and "no production score" in early.breaches[0]

    _measure(db, {}, 72.0, baseline=True)  # validation's production run
    _measure(db, {}, 68.0)  # production as a recipe campaign's empty config
    assert quality.production_scores(db, campaign, [GPQA]) == {
        GPQA: pytest.approx(0.70)
    }
    assert quality.rescore(db, campaign, lambda run: OBJECTIVE) >= 1
    assert early.feasible  # 0.62 >= 0.70 - 0.10

    broken = _measure(db, {"enable_attn_tp_input_scattered": True}, 25.0)
    assert not broken.feasible
    assert broken.breaches == [
        f"{GPQA}=0.25 >= 0.6 (production 0.7 - 0.1)"
    ]


def test_a_finalist_without_a_quality_score_does_not_pass(db):
    _measure(db, {}, 70.0, baseline=True)
    campaign = db.get(Campaign, 1)
    reader = reader_for({"replay.input_tpm": 1.0, "replay.output_tpm": 1.0}, {}, {"x": 1},
                        campaign)
    summary = quality.judge(db, campaign, OBJECTIVE, reader)
    assert not summary.feasible and "missing" in summary.breaches[0]


def test_production_landing_judges_the_finalists_again(db):
    """Through the supervisor: a finalist stored before production fails the
    floor; production's result arriving judges it again."""
    from app.control.orchestrator.supervisor import Supervisor
    from app.evaluation.base import EvalOutcome, EvalStatus

    def candidate(n, config, baseline=False):
        db.add(Candidate(id=n, campaign_id=1, config=config, config_hash=f"h{n}",
                         kind="verification", is_baseline=baseline))
        db.add(Run(id=n, campaign_id=1, candidate_id=n))
        db.flush()
        return db.get(Run, n)

    def outcome(gpqa):
        return EvalOutcome(status=EvalStatus.PASSED, metrics={
            "replay.input_tpm": 700_000.0, "replay.output_tpm": 20_000.0,
            "opencompass.gpqa_diamond": gpqa})

    supervisor = Supervisor.__new__(Supervisor)  # only _record_result is used
    supervisor._check_dataset = lambda *a, **k: None
    finalist = candidate(1, {"speculative_algorithm": "NEXTN"})
    supervisor._record_result(db, finalist, "llmbench", outcome(66.0))
    db.flush()
    stored = db.query(Result).filter_by(run_id=1).one()
    assert not stored.feasible

    supervisor._record_result(db, candidate(2, {}, baseline=True), "llmbench", outcome(70.0))
    db.flush()
    db.refresh(stored)
    assert stored.feasible  # 0.66 >= 0.70 - 0.10


def test_a_config_that_leaves_the_recipe_as_it_is_is_production(db):
    """A space lists configs side by side with null for a flag one lacks;
    the all-null row is production, a row that sets something is not."""
    campaign = db.get(Campaign, 1)
    null_row = Candidate(config={"speculative_algorithm": None, "dsa_decode_backend": None})
    assert quality.is_production(null_row, campaign)
    assert quality.is_production(Candidate(config={}), campaign)
    assert not quality.is_production(Candidate(config={"speculative_algorithm": "NEXTN",
                                                       "dsa_decode_backend": None}), campaign)
    # Removing a flag the recipe does set is a change, not production.
    assert not quality.is_production(Candidate(config={"tp": None}), campaign)
