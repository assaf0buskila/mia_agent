import pytest
from app.capabilities.policy import authorize, is_safe_read
from app.capabilities.types import Principal
from app.core.errors import PermissionDenied


def test_safe_reads_need_no_confirmation() -> None:
    assert is_safe_read("mail.read") is True
    assert is_safe_read("calendar.get_schedule") is True
    assert is_safe_read("leads.get_recent") is True
    assert is_safe_read("memory.search") is True
    assert is_safe_read("knowledge.search") is True
    authorize("mail.read", principal=Principal.owner(source="test"))
    authorize("calendar.get_schedule", principal=Principal.owner(source="test"))
    authorize("leads.get_recent", principal=Principal.owner(source="test"))
    authorize("memory.search", principal=Principal.owner(source="test"))
    authorize("knowledge.search", principal=Principal.owner(source="test"))
    authorize("knowledge.search", principal=Principal.client(source="test"))
    authorize("business.get_information", principal=Principal.client(source="test"))


def test_destructive_mail_delete_is_denied() -> None:
    with pytest.raises(PermissionDenied):
        authorize("mail.delete", principal=Principal.owner(source="test"))


def test_kill_switch_blocks_reads() -> None:
    with pytest.raises(PermissionDenied):
        authorize("mail.read", principal=Principal.owner(source="test"), kill_switch=True)


def test_draft_write_is_not_a_read() -> None:
    assert is_safe_read("mail.create_draft") is False
    authorize("mail.create_draft", principal=Principal.owner(source="test"))
