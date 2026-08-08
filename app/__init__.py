"""Coding-agent backend.

A modular monolith: every feature is a separate package with strict
boundaries so individual services can be extracted later if load demands.

Phases:
    - gateway:      HTTP/WebSocket entry point (done)
    - database:     persistence + migrations (done)
    - orchestrator: agent execution graph (Phase 5)
    - agents:       planner/coder/reviewer/tester/debug/deploy (Phase 5-8)
    - tools:        typed tool specs (Phase 4)
    - executor:     sandboxed tool execution (Phase 4)
    - retrieval:    indexing + semantic search (Phase 9)
    - memory:       conversation/repo/preferences (Phase 10)
    - llm:          provider abstraction + context management (Phase 5)
    - evals:        task harness + regression suite (Phase 11)
    - monitoring:   OTel traces, metrics, structured logs (Phase 11)
"""
