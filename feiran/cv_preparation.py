"""Shared reword/layout sequence; callers retain identity, persistence and transactions.

Each yielded result describes one completed attempt. A model failure keeps the last usable
input for the next step. Local edits are applied to outputs, never fed back into the model.
The file adapter may persist each result; SQLite collects them for one guarded publication.
"""
from dataclasses import dataclass
import time
from typing import Callable, Iterator

from feiran.cv import CVError
from feiran.cv_edit import carry_over
from feiran.cv_status import adjusted, reworded, stage_failure


@dataclass
class PreparationStep:
    kind: str
    document: dict | None
    status: dict
    started: float


def prepare_steps(draft: dict, facts, job: dict, *, chat, private, previous: dict | None,
                  tailor: Callable, plan: Callable, on_stage: Callable = lambda stage: None
                  ) -> Iterator[PreparationStep]:
    """Run the two model stages, yielding successes or explicit fallback status.

    Only CVError is a usable-content fallback. Storage, cancellation and unexpected errors
    remain the adapter's responsibility and propagate without being labelled success.
    """
    head = draft
    for stage, kind in (("rewording", "tailored"), ("layout", "planned")):
        on_stage(stage)
        started = time.monotonic()
        try:
            if stage == "rewording":
                head = tailor(head, facts, job=job, chat=chat, effort="none", private=private)
                status = reworded(head)
            else:
                head = plan(head, job, chat=chat, private=private)
                status = adjusted(head)
        except CVError as exc:
            yield PreparationStep(kind, None, stage_failure(stage, exc), started)
        else:
            yield PreparationStep(kind, carry_over(previous, head), status, started)
