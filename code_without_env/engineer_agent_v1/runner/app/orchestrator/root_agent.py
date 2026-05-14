"""ADK root agent for the standalone EngineerAgent service.

This deployment contains only EngineerAgent. It receives DetectiveAgent RCA
payloads over HTTP and forwards TMF921 execution payloads to ExecutorAgent by
URL.
"""

from app.engineer_agent.agent import engineer_agent


root_agent = engineer_agent
