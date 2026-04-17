from google.adk.agents import Agent
from .tools import validate_remediation


# validation_agent = LlmAgent(
#     model="gemini-2.0-flash",
#     name="ValidationAgent",
#     description="Validates post-remediation KPI recovery and confirms z-score normalization to baseline.",
#     instruction="""
#     You are the Validation Agent.
#     Validate whether z-score returned to healthy baseline.You MUST call the tool `validate_remediation`.
#     Do NOT generate text.
#     Do NOT explain anything.

#     Only call the tool and return its JSON output.
#     Always return valid JSON.
#     """,
#     tools=[validate_remediation],
# )

class ValidationAgent(Agent):
    def run(self, context):
        print("Running ValidationAgent")
        return validate_remediation(context)


validation_agent = ValidationAgent(
    name="ValidationAgent",
)