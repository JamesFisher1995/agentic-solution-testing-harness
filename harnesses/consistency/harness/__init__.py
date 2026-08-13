"""Consistency harness for the Agentic AI STLC Impact Analyser.

A standalone multi-agent system that drives the running Impact Analyser over its
HTTP API, runs the *same* change request through the analyser N times, and scores
how consistent the resulting impact reports are across runs.

It never imports application internals — it only speaks HTTP to:
  * agent-bus-api      (:8000)  — the analyser pipeline
  * integration-service(:3001)  — optional, to fetch a CR + its tests from ADO

See README.md for the design and the runbook.
"""

__version__ = "0.1.0"
