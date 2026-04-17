from google.adk.agents import Agent
from .tools import generate_healing_plan


# solution_planning_agent = LlmAgent(
#     model="gemini-2.0-flash",
#     name="SolutionPlanningAgent",
#     description="Generates ranked remediation actions and TMF921 intent payloads from confirmed root cause hypotheses.",
#     instruction="""
#     You are the Solution Planning Agent.
#     Generate ranked healing actions from RCA-confirmed cause.You MUST call the tool `generate_healing_plan`.
#     Do NOT generate text.
#     Do NOT explain anything.

#     Only call the tool and return its JSON output.
#     Always return valid JSON.
#     """,
#     tools=[generate_healing_plan],
# )

class SolutionPlanningAgent(Agent):
    def run(self, context):
        print("Running SolutionPlanningAgent")
        return generate_healing_plan(context)


solution_planning_agent = SolutionPlanningAgent(
    name="SolutionPlanningAgent",
)