"""Each invalid fixture must raise StrategySpecValidationErrorGroup with
at least one error matching the expected error_code, with field_path
containing the expected substring and message containing the expected
substring. error_code must be exact; field_path and message use
substring containment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from marketmind_shared.schemas.strategy_spec import (
    StrategySpecValidationErrorGroup,
    validate_spec,
)

from .conftest import expected_error_for, load_json


def test_invalid_fixture_raises_expected_error(invalid_fixture_path: Path) -> None:
    data = load_json(invalid_fixture_path)
    expected = expected_error_for(invalid_fixture_path)

    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(data)

    errors = excinfo.value.errors
    assert errors, "error group is empty"

    expected_code = expected["error_code"]
    expected_path_sub = expected["field_path"]
    expected_msg_sub = expected["message_contains"]

    matching = [
        err
        for err in errors
        if err.error_code == expected_code
        and expected_path_sub in err.field_path
        and expected_msg_sub in err.message
    ]
    assert matching, (
        f"no error matched expected (code={expected_code!r}, "
        f"path_substring={expected_path_sub!r}, msg_substring={expected_msg_sub!r}); "
        f"actual errors: {[str(e) for e in errors]}"
    )
