# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""AG-UI snapshot event shape tests."""

from app.services.agui_interrupt import (
    build_persist_snapshot_events,
    build_run_finished_interrupt_event,
)
from app.services.agui_messages import litellm_messages_to_agui_snapshot


def test_litellm_to_agui_snapshot_roles():
    msgs = litellm_messages_to_agui_snapshot([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "Tool", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ])
    roles = [m.role for m in msgs]
    assert roles == ["system", "user", "assistant", "tool"]


def test_persist_snapshot_events_shape():
    state = {
        "messages": [{"role": "user", "content": "x"}],
        "executionGuid": "exec-1",
        "stateGuid": "state-1",
        "harness_manifest": {"orchestrationType": "cursor"},
    }
    pending = [{"tool_call_id": "call_a", "interrupt_id": "call_a"}]
    events = build_persist_snapshot_events(state_payload=state, pending_tools=pending)
    assert len(events) == 2
    assert events[0].type.value == "MESSAGES_SNAPSHOT"
    assert events[1].type.value == "STATE_SNAPSHOT"
    assert events[1].snapshot["pendingToolCallIds"] == ["call_a"]
    assert events[1].snapshot["interruptIds"] == ["call_a"]


def test_run_finished_interrupt_ids_match_pending():
    evt = build_run_finished_interrupt_event(
        thread_id="t",
        run_id="r",
        pending_tools=[{"tool_call_id": "call_a"}, {"tool_call_id": "call_b"}],
    )
    dumped = evt.model_dump(by_alias=True)
    ids = {i["id"] for i in dumped["outcome"]["interrupts"]}
    assert ids == {"call_a", "call_b"}
