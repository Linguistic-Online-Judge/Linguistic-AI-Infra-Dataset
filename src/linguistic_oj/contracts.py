"""Versioned task contracts shared by challenge building and evaluation."""

from collections.abc import Mapping
from types import MappingProxyType

from .responses import TaskType

SCORER_VERSION = "1.0"
AGGREGATION_VERSION = "1.0"

TASK_METRICS: Mapping[TaskType, tuple[str, tuple[str, ...]]] = MappingProxyType({
    TaskType.SEGMENTATION: ("micro_f1", ("micro_precision", "micro_recall")),
    TaskType.UPOS: ("micro_accuracy", ()),
    TaskType.XPOS: ("micro_accuracy", ()),
    TaskType.DEPENDENCY: ("las", ("uas",)),
    TaskType.TRANSLITERATION: (
        "token_accuracy",
        ("sentence_exact_match_rate",),
    ),
})

RESPONSE_SCHEMA_VERSIONS: Mapping[TaskType, str] = MappingProxyType(
    {task: f"{task.value}-v1" for task in TaskType}
)
