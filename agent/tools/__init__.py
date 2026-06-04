"""Tools the agent can use during a run.

Tools are *not* directly exposed to the LLM as JSON-schema tools right
now. Stages call them programmatically. This keeps the agent's
decision-making in our orchestrator rather than in the model — the model
decides WHAT to change; the orchestrator decides WHEN and HOW to read
files, search, validate, etc.

If we later want to give the Editor real tool-use, the tools here are
already wrapped as plain Python functions that return strings — adding
a `tool_definitions.py` wrapper to expose them is a small follow-up.
"""
