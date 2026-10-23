"""Source-kind extension exercises the existing pure preparation path."""

from types import SimpleNamespace

import pytest
from app.prototypes.assaf_os_primary import LocalBinding, LocalPrimaryError
from app.prototypes.assaf_os_shadow_import import LocalShadowImporter
from app.prototypes.assaf_os_synthetic import make_synthetic_batch


@pytest.mark.parametrize("prefix", ["synthetic:", "owner_export:"], ids=["synthetic", "owner"])
def test_os_import_source_kind_accepted(prefix):
    batch = make_synthetic_batch("synthetic-workspace")
    batch["source_id"] = prefix + "fixture"
    # _prepare has no engine/DB dependency. Do not construct a persistence service.
    host = SimpleNamespace(binding=LocalBinding("synthetic-workspace", "12345"))
    prepared = LocalShadowImporter._prepare(host, batch)
    assert len(prepared.records) == 12
    assert not prepared.initial_issues


def test_os_import_source_kind_other_refused():
    batch = make_synthetic_batch("synthetic-workspace")
    batch["source_id"] = "unsupported:fixture"
    host = SimpleNamespace(binding=LocalBinding("synthetic-workspace", "12345"))
    with pytest.raises(LocalPrimaryError, match="^synthetic_source_required$"):
        LocalShadowImporter._prepare(host, batch)
