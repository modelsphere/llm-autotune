"""Bootstrap the baselines table from what machines already have captured.

Capture upserts a baseline on the next hand-over, but a machine captured before
that code existed has a production inventory and no baseline row. This restores
the reference from that existing knowledge — parsing each captured service's
command into engine args — so campaigns have something to compare against
without waiting for a re-capture.

Idempotent and conservative: it creates one baseline per (served model, engine,
card type), and never overwrites a baseline a human set by hand (source
"manual"). Run on the dev box after deploying the baselines migrations:

    python scripts/seed_baselines_from_capture.py            # every captured machine
    python scripts/seed_baselines_from_capture.py gpu-a      # just this one
"""

import sys

sys.path.insert(0, "/root/llm_autotune/backend")
sys.path.insert(0, "/root/sjc/llm_autotune/backend")

from app.control.engine_command import baseline_engine_args, engine_of_command  # noqa: E402
from app.db.base import sync_session_factory  # noqa: E402
from app.db.models import Baseline, Machine  # noqa: E402
from sqlalchemy import select  # noqa: E402

only = sys.argv[1] if len(sys.argv) > 1 else None


def service_config(service: dict) -> dict:
    return service.get("engine_args") or baseline_engine_args(
        service.get("command") or service.get("docker_run")
    )


with sync_session_factory() as session:
    machines = session.scalars(select(Machine)).all()
    created = updated = skipped = 0
    for machine in machines:
        if only and machine.name != only:
            continue
        services = (machine.baseline or {}).get("services", [])
        card_type = machine.gpu_type or ""
        for service in services:
            model = service.get("served_model_name")
            if not model:
                continue
            engine = engine_of_command(
                service.get("command") or service.get("docker_run")
            ) or "sglang"
            config = service_config(service)
            existing = session.scalars(
                select(Baseline).where(
                    Baseline.served_model_name == model,
                    Baseline.engine == engine,
                    Baseline.card_type == card_type,
                )
            ).first()
            label = f"{model} / {engine} / {card_type or '(any card)'}"
            if existing is None:
                session.add(Baseline(
                    served_model_name=model, engine=engine, card_type=card_type,
                    engine_args=config, source=f"capture:{machine.name}",
                ))
                created += 1
                print(f"  + {label}  <- {machine.name}: {config}")
            elif existing.source.startswith("capture"):
                existing.engine_args = config
                existing.source = f"capture:{machine.name}"
                updated += 1
                print(f"  ~ {label}  (refreshed from {machine.name})")
            else:
                skipped += 1
                print(f"  = {label}  (left alone — set by hand)")
    session.commit()
    print(f"\ncreated {created}, refreshed {updated}, left {skipped} manual row(s) alone")
