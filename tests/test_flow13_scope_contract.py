"""Scope and shared-resource contracts for parallel Work selection."""

import pytest

from relay.work_parallel import overlap, resources


def test_windows_separators_and_case_are_normalized():
    assert overlap([r"Src\Package\File.py"], ["src/package/file.py"])
    assert overlap([r"SRC\Package"], ["src/package/file.py"])


def test_ancestor_overlap_in_both_orders():
    parent = ["src/package"]
    child = ["src/package/sub/file.py"]
    assert overlap(parent, child)
    assert overlap(child, parent)


def test_adjacent_path_prefixes_are_independent():
    assert not overlap(["src/package"], ["src/package-extra/file.py"])
    assert not overlap(["src/package/file.py"], ["src/package/file.py.bak"])


def test_independent_files_do_not_overlap():
    assert not overlap(["src/alpha.py"], ["src/beta.py"])
    assert not overlap([], ["src/alpha.py"])


def test_absent_shared_resources_are_empty():
    assert resources({"id": "work-without-metrics"}) == set()
    assert resources({"id": "work-without-declaration", "metrics": {}}) == set()


def test_valid_shared_resources_are_returned_as_a_set():
    assert resources({"id": "work", "metrics": {"shared_resources": ["gpu", "database", "gpu"]}}) == {"gpu", "database"}
    assert resources({"id": "work", "metrics": {"shared_resources": []}}) == set()


@pytest.mark.parametrize("declaration", [None, "gpu", {"gpu": True}, 1, [""], ["  "], [42], ["gpu", None]])
def test_invalid_shared_resources_raise_value_error(declaration):
    with pytest.raises(ValueError, match="Invalid shared_resources for invalid-work"):
        resources({"id": "invalid-work", "metrics": {"shared_resources": declaration}})
