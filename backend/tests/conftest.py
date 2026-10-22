class DisabledLLM:
    is_configured = False

    def complete(self, messages):
        raise AssertionError("LLM should be disabled for this test")
