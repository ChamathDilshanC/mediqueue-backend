from backend.main import Transition

def test_transition_payload_supports_optimistic_version():
    assert Transition(expected_version=3).expected_version == 3
