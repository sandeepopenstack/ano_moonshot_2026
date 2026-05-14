"""ADK root agent for the standalone ReflexAgent service.

This deployment contains only ReflexAgent. Downstream coordination with
DetectiveAgent, EngineerAgent, ExecutorAgent, and ReflectionAgent happens by
HTTP A2A payloads, not by importing those agents in-process.
"""

from app.reflex_agent.agent import reflex_agent


root_agent = reflex_agent
