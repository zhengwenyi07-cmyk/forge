from hello import hello


def test_hello_returns_expected_string() -> None:
    """Test that hello() returns the expected greeting string."""
    result = hello()
    assert result == "hello from agent!", f"Expected 'hello from agent!', got '{result}'"
