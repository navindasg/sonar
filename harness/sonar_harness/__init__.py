"""Sonar harness package.

The tool-using brain between STT and TTS: an OpenAI-compatible /v1 server
that runs a bounded tool loop against local gemma (Ollama) and the RAG MCP
tools. See CONTRACTS.md for the three shared interfaces the parallel build
streams must not diverge on.
"""
