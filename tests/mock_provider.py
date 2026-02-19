from typing import List, Dict

class MockLLMProvider:
    def __init__(self, responses: List[str]):
        self.responses = responses
        self.call_count = 0

    def chat(self, messages: List[Dict[str, str]], system: str = None, temperature: float = 0.0) -> str:
        if self.call_count < len(self.responses):
            response = self.responses[self.call_count]
            self.call_count += 1
            return response
        return "NO MORE RESPONSES"
