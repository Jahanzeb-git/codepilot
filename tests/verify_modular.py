import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from codepilot.engine.runtime import Runtime
from tests.mock_provider import MockLLMProvider

# 1. Setup a Test Agent Config
agent_yaml = "test_agent_modular.yaml"
# Ensure workspace exists for absolute path resolution in tests
abs_workspace = os.path.abspath("./workspace_modular")
os.makedirs(abs_workspace, exist_ok=True)

with open(agent_yaml, "w") as f:
    f.write(f"""
agent:
  name: "ModularTestBot"
  system_prompt: "You are a test bot."
  model:
    provider: "openai"
    name: "gpt-4o"
  runtime:
    work_dir: "{abs_workspace.replace(os.sep, '/')}"
    unsafe_mode: true
""")

# 2. Define the LLM Response
# We simulate a response that uses the new modular tools
llm_response = """
I will test the modular filesystem tools.

```python
from tools import write_file, read_file, think

think("Writing a file using the new modular tool.")
write_file("modular.txt")
```
```python
Line 1
Line 2
Line 3
```

```python
from tools import read_file
# Read with line numbers
content = read_file("modular.txt", start_line=1, end_line=2)
print(f"Read Content:\\n{content}")
```
"""

# 3. Initialize Runtime with Mock Provider
# Note: We need to mock the PromptManager or ensure the template exists.
# Since we created the template in the previous step, it should be fine.
runtime = Runtime(agent_file=agent_yaml)
runtime.provider = MockLLMProvider([llm_response])

# 4. Run
print("Starting Modular Verification Run...")
try:
    runtime.run("Verify modular tools.")
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"FAILED with error: {e}")

# 5. Check Result
file_path = os.path.join(abs_workspace, "modular.txt")

if os.path.exists(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    expected = "Line 1\nLine 2\nLine 3"
    
    print("\n--- RESULT ---")
    print(f"File content:\n{content}")
    
    # We might have extra newlines depending on how we handled it in filesystem.py
    if content.strip() == expected.strip():
        print("\nSUCCESS: Modular write_file worked!")
    else:
        print("\nFAILURE: Content mismatch.")
else:
    print("\nFAILURE: File was not created.")
