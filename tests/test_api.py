from __future__ import annotations

import pytest

from parity.api import check


def test_check_rejects_explicit_empty_selection_before_loading_config() -> None:
    with pytest.raises(ValueError, match="cases must contain at least one case name"):
        check("missing.toml", cases=set())
