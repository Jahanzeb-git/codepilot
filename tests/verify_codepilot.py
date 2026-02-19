import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from codepilot.engine.runtime import Runtime
from codepilot.core.agent_file import AgentFile, ModelConfig, RuntimeConfig
from tests.mock_provider import MockLLMProvider

# 1. Setup a Test Agent Config
agent_yaml = "test_agent.yaml"
with open(agent_yaml, "w") as f:
    f.write("""
agent:
  name: "TestBot"
  system_prompt: "You are a test bot."
  model:
    provider: "openai"
    name: "gpt-4o"
  runtime:
    work_dir: "./workspace"
    unsafe_mode: true
""")

# 2. Create Workspace
os.makedirs("./workspace", exist_ok=True)

# 3. Define the LLM Response (The "Side-Loading" payload)
llm_response = """
I will write a python script with a complex string to verify side-loading.

```python
from tools import write_file, think

think("Writing a file with complex strings.")
write_file("complex.py")
```

```python
def get_string():
    return "This string has \\"nested quotes\\" and \\n newlines!"
```
"""

# 4. Initialize Runtime with Mock Provider
runtime = Runtime(agent_file=agent_yaml)
runtime.provider = MockLLMProvider([llm_response])

# 5. Run
print("Starting Verification Run...")
runtime.run("Verify side-loading.")

# 6. Check Result
file_path = "./workspace/complex.py"
if os.path.exists(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    expected = 'def get_string():\n    return "This string has \\"nested quotes\\" and \\n newlines!"\n'
    
    print("\n--- RESULT ---")
    print(f"File content:\n{content}")
    
    if content.strip() == expected.strip():
        print("\nSUCCESS: Side-loading worked correctly!")
    else:
        print("\nFAILURE: Content mismatch.")
else:
    print("\nFAILURE: File was not created.")
