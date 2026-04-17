from google.adk.agents import Agent
from .tools import monitor_and_triage

# monitoring_agent = LlmAgent(
#     model="gemini-2.0-flash",
#     name="MonitoringAgent",
#     description="Consumes GNN anomaly signals, performs domain triage, and prioritizes issues by business impact.",
#     instruction="""
#     You are the Monitoring and Detection Agent.
#     Consume GNN inference outputs.
#     You MUST call the tool `monitor_and_triage` to Perform domain triage and priority ranking.
#     Do NOT generate text.
#     Do NOT explain anything.

#     Only call the tool and return its JSON output.
#     Always return valid JSON.
#     """,
#     tools=[monitor_and_triage],
# )



class MonitoringAgent(Agent):
    def run(self, context):
        print("Running MonitoringAgent")  # debug
        return monitor_and_triage(context)


monitoring_agent = MonitoringAgent(
    name="MonitoringAgent",
    description="Consumes GNN anomaly signals and performs triage",
)