import pytest
from app.capabilities.policy import authorize, is_safe_read
from app.capabilities.types import GraphName
from app.core.errors import PermissionDenied


def test_safe_reads_need_no_confirmation() -> None:
    assert is_safe_read("mail.read") is True
    assert is_safe_read("calendar.get_schedule") is True
    assert is_safe_read("leads.get_recent") is True
    assert is_safe_read("memory.search") is True
    assert is_safe_read("knowledge.search") is True
    authorize("mail.read", graph=GraphName.OWNER)
    authorize("calendar.get_schedule", graph=GraphName.OWNER)
    authorize("leads.get_recent", graph=GraphName.OWNER)
    authorize("memory.search", graph=GraphName.OWNER)
    authorize("knowledge.search", graph=GraphName.OWNER)
    authorize("knowledge.search", graph=GraphName.CLIENT)
    authorize("business.get_information", graph=GraphName.CLIENT)


def test_destructive_mail_delete_is_denied() -> None:
    with pytest.raises(PermissionDenied):
        authorize("mail.delete", graph=GraphName.OWNER)


def test_kill_switch_blocks_reads() -> None:
    with pytest.raises(PermissionDenied):
        authorize("mail.read", graph=GraphName.OWNER, kill_switch=True)


def test_draft_write_is_not_a_read() -> None:
    assert is_safe_read("mail.create_draft") is False
    authorize("mail.create_draft", graph=GraphName.OWNER)
