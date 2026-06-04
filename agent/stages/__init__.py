"""Pipeline stages. Each module exports one top-level function.

The orchestrator (`agent.loop.run`) is the only caller of these — stages
do not call each other.
"""
