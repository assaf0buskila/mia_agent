import re
from pathlib import Path

from app.core.capabilities import CapabilityId, require_alive
from app.domain.policies.task_classes import (
    ALLOWLISTED_MODEL_SOURCES,
    ALLOWLISTED_OWNERS,
    TaskClass,
    task_class_pin,
)

_MODEL_BRAND_RE = re.compile(r"(?i)gpt-|claude|gemini|grok|o1-")

_ALL_TASK_CLASSES = tuple(TaskClass)


def test_all_task_class_names_pin() -> None:
    for task_class in _ALL_TASK_CLASSES:
        pin = task_class_pin(task_class.value)
        assert pin.task_class == task_class.value
        assert pin.owner in ALLOWLISTED_OWNERS
        assert pin.model_source in ALLOWLISTED_MODEL_SOURCES


def test_unknown_name_fail_closed_code_none() -> None:
    pin = task_class_pin("not_a_real_class")
    assert pin.task_class == "not_a_real_class"
    assert pin.owner == "code"
    assert pin.model_source == "none"
    assert pin.benchmark_later is False


def test_no_pin_contains_model_brand_tokens() -> None:
    for task_class in _ALL_TASK_CLASSES:
        pin = task_class_pin(task_class.value)
        assert _MODEL_BRAND_RE.search(pin.owner) is None
        assert _MODEL_BRAND_RE.search(pin.model_source) is None
        if pin.notes:
            assert _MODEL_BRAND_RE.search(pin.notes) is None


def test_transcribe_and_normal_sales_use_env_model_source() -> None:
    assert task_class_pin(TaskClass.TRANSCRIBE.value).model_source == "env"
    assert task_class_pin(TaskClass.NORMAL_SALES_CONVERSATION.value).model_source == "env"
    assert task_class_pin(TaskClass.OWNER_CONVERSATION.value).model_source == "env"


def test_code_owned_classes_use_none_model_source() -> None:
    none_sources = (
        TaskClass.ROUTE,
        TaskClass.EXTRACT,
        TaskClass.CAMPAIGN_INTERPRETATION,
        TaskClass.DEEP_RESEARCH,
        TaskClass.MESSAGE_HUMANITY_REVIEW,
        TaskClass.SAFETY_VERIFICATION,
        TaskClass.OBJECTION_HANDLING,
    )
    for task_class in none_sources:
        assert task_class_pin(task_class.value).model_source == "none"


def test_model_task_classes_capability_alive() -> None:
    require_alive(CapabilityId.MODEL_TASK_CLASSES)


def test_inbound_and_graph_do_not_import_task_class_pin() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for relative in ("app/api/inbound.py", "app/graph/orchestrator.py"):
        source = (repo_root / relative).read_text(encoding="utf-8")
        assert "task_class_pin" not in source
