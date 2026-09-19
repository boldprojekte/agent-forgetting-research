"""context_apply: in-place stubs, archive-before-stub, stable result IDs, atomic failure."""

import copy

import pytest

from forgetting_agent.context import Archive, ContextState


def call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def make_state() -> ContextState:
    state = ContextState(system_prompt="sys", user_task="task")
    state.append_assistant(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "thinking about a",
            "tool_calls": [call("c1", "read_file", '{"path": "a.md"}')],
        }
    )
    state.append_tool_result("c1", "read_file", {"path": "a.md"}, "Alpha flood level 42.30 m.")
    state.append_assistant(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [call("c2", "read_file", '{"path": "b.md"}')],
        }
    )
    state.append_tool_result(
        "c2", "read_file", {"path": "b.md"}, "Beta regulations 600 mm freeboard."
    )
    return state


def test_apply_keeps_pairs_and_archives_raw_original():
    state = make_state()
    before = copy.deepcopy(state.messages)
    assert state.apply([{"id": "r1", "note": "FFL rule kept"}]).ok
    assert len(state.messages) == len(before)
    assert state.messages[:3] == before[:3]
    assert state.messages[4:] == before[4:]
    assert state.messages[3]["tool_call_id"] == "c1"
    assert state.messages[3]["content"].startswith("[set aside:")
    assert state.archive.get("ic-0001").original_text == "Alpha flood level 42.30 m."
    assert state.archive.get("ic-0001").record.result_id == "r1"


def test_storage_failure_is_atomic(monkeypatch):
    state = make_state()
    before = copy.deepcopy(state.__dict__)

    def fail(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(Archive, "with_entries", fail)
    assert not state.apply([{"id": "r1", "note": "kept"}]).ok
    assert state.__dict__ == before


@pytest.mark.parametrize(
    "targets",
    [
        [],
        None,
        [{"id": "r999", "note": "n"}],
        [{"id": "m1.1", "note": "n"}],
        [{"id": "r1", "note": ""}],
        [{"id": "r1", "note": None}],
        [{"id": "r1", "note": 42}],
        [{"id": None, "note": "n"}],
        [{"id": "r1", "note": "a"}, {"id": "r1", "note": "b"}],
        [{"id": "r1", "note": "valid"}, {"id": "r999", "note": "unknown"}],
    ],
)
def test_invalid_batch_preserves_all_state(targets):
    state = make_state()
    before = copy.deepcopy(state.__dict__)
    assert not state.apply(targets).ok
    assert state.__dict__ == before


def test_archived_or_cleared_id_cannot_select_another_result():
    state = make_state()
    assert state.apply([{"id": "r1", "note": "first"}]).ok
    before = copy.deepcopy(state.__dict__)
    assert not state.apply([{"id": "r1", "note": "again"}]).ok
    assert state.__dict__ == before
    # An unused result keeps its ID after another output is archived.
    assert state.apply([{"id": "r2", "note": "second"}]).ok
    state = make_state()
    state.clear_oldest_result()
    assert not state.apply([{"id": "r1", "note": "cleared"}]).ok


def test_apply_receipt_maps_selected_result_to_actual_recovery_reference():
    state = make_state()
    result = state.apply([{"id": "r2", "note": "600 mm"}])
    assert result.ok and "r2 -> ic-0001" in result.text
    assert state.archive.get("ic-0001").original_text == "Beta regulations 600 mm freeboard."


def test_invalid_batch_explains_every_target_and_preserves_valid_outputs():
    state = make_state()
    assert state.apply([{"id": "r1", "note": "flood rule"}]).ok
    before = copy.deepcopy(state.__dict__)
    result = state.apply(
        [
            {"id": "r1", "note": "again"},
            {"id": "r2", "note": "600 mm"},
            {"id": "r3", "note": "not yet issued"},
        ]
    )
    assert not result.ok
    assert "r1: ALREADY ARCHIVED" in result.text and "ic-0001" in result.text
    assert "r2: ACTIVE" in result.text and "b.md" in result.text
    assert "r3: NOT ISSUED" in result.text and "Last issued result ID: r2" in result.text
    assert "NOTHING was archived" in result.text
    assert state.__dict__ == before
    assert state.apply([{"id": "r2", "note": "600 mm"}]).ok
    assert state.archive.get("ic-0002").original_text == "Beta regulations 600 mm freeboard."
