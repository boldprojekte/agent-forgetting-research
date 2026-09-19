"""Recovery preserves original bytes and produces a separately addressable result."""

import copy

from tests.test_context_apply import make_state


def test_recovery_preserves_stub_and_assigns_fresh_id():
    state = make_state()
    original = "Alpha flood level 42.30 m."
    assert state.apply([{"id": "r1", "note": "kept"}]).ok
    before = copy.deepcopy(state.messages)
    recovered = state.recover("ic-0001")
    assert recovered.ok and original in recovered.text
    assert '<context_result id="r1">' not in recovered.text
    assert state.messages == before
    state.append_tool_result(
        "recovery-call", "context_recover", {"ref": "ic-0001"}, recovered.text, kind="recovered"
    )
    assert state.messages[: len(before)] == before
    assert state.records["recovery-call"].result_id == "r3"
    assert state.apply([{"id": "r3", "note": "seen twice"}]).ok
    assert state.archive.get("ic-0002").original_text == recovered.text
    assert state.archive.get("ic-0001").original_text == original
    assert state.recover("ic-0001").text == recovered.text


def test_unknown_reference_does_not_expose_original():
    state = make_state()
    assert state.apply([{"id": "r1", "note": "kept"}]).ok
    result = state.recover("ic-9999")
    assert not result.ok and "ic-0001" in result.text
    assert "Alpha flood" not in result.text
