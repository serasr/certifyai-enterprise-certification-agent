"""
Multi-Agent Orchestrator using Microsoft Agent Framework
Sequential workflow: Orchestrator -> Learning Path Curator -> 
Study Plan Generator -> Assessment Agent -> Engagement Agent
"""
import asyncio
import os
from agent_framework import Agent, Message
from agent_framework.foundry import FoundryChatClient
from agent_framework.orchestrations import SequentialBuilder
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

load_dotenv(r"E:\my lap backup\d drive\Sera docs\GIT\agents-league\enterprise-cert-agent\.azure\enterprise-cert-agent\.env")

ENDPOINT = os.environ.get("AZURE_EXISTING_AIPROJECT_ENDPOINT")

async def run_certification_workflow(user_query: str) -> str:
    client = FoundryChatClient(
        credential=AzureCliCredential(),
        project_endpoint=ENDPOINT,
        model=os.environ.get("AZURE_AI_AGENT_DEPLOYMENT_NAME", "gpt-4o"),
    )

    # Step 1: Learning Path Curator
    learning_path_curator = Agent(
        client=client,
        name="LearningPathCurator",
        instructions="""You are the Learning Path Curator for an enterprise certification system.
Given an employee role and target certification, retrieve and recommend:
1. Required certifications for the role
2. Recommended study hours
3. Prerequisites
4. Logical study sequence
Always cite source documents. Only use knowledge from these roles: Cloud Engineer (AZ-204, AZ-305), 
DevOps Engineer (AZ-400, AZ-104), Data Engineer (DP-203, DP-900).
Pass your findings to the next agent."""
    )

    # Step 2: Study Plan Generator
    study_plan_generator = Agent(
        client=client,
        name="StudyPlanGenerator",
        instructions="""You are the Study Plan Generator for an enterprise certification system.
Given the learning path from the previous agent, create a capacity-aware study plan.
Use these risk thresholds:
- HIGH RISK: focus hours below 10, study hours below 15, practice score below 65
- MODERATE RISK: focus hours below 12, study hours below 20, practice score below 75
Generate: weekly schedule, milestone checkpoints, risk assessment, exam date recommendation.
Always flag HIGH RISK employees for manager review.
Pass your findings to the next agent."""
    )

    # Step 3: Assessment Agent
    assessment_agent = Agent(
        client=client,
        name="AssessmentAgent",
        instructions="""You are the Assessment Agent for an enterprise certification system.
Given the learning path and study plan from previous agents, evaluate readiness:
1. Compare current status against pass threshold (75%)
2. Generate 3-5 practice questions for the target certification
3. Provide READY, MODERATE RISK, or HIGH RISK classification
4. Recommend next action (proceed to exam or return to study)
Responsible AI: Never generate questions outside approved certification content.
Never make promises about exam outcomes.
Pass your findings to the next agent."""
    )

    # Step 4: Engagement Agent
    engagement_agent = Agent(
        client=client,
        name="EngagementAgent",
        instructions="""You are the Engagement Agent for an enterprise certification system.
Given all previous agent findings, create an engagement and reminder plan:
1. Suggest optimal study slots based on workload signals
2. Set reminder frequency based on risk level:
   - HIGH RISK: daily reminders, escalate to manager
   - MODERATE RISK: every other day reminders  
   - LOW RISK: weekly check-ins
3. Recommend specific morning/afternoon/evening slots
Responsible AI: Never access real calendar data. Only recommend, never send actual messages.
Synthesize all previous findings into a final comprehensive response for the user.
Add "Access logged for audit purposes" at the end.
End with "Is there anything else I can help you with?" """
    )

    # Build workflow
    workflow = SequentialBuilder(
        participants=[
            learning_path_curator,
            study_plan_generator,
            assessment_agent,
            engagement_agent,
        ]
    ).build()

    # Run the workflow
    print(f"Running certification workflow for query: {user_query[:100]}...")
    run_result = await workflow.run(user_query)
    
    # Extract the final output from the EngagementAgent
    for event in run_result:
        if event.type == "output":
            agent_response = event.data
            if hasattr(agent_response, "content"):
                return str(agent_response.content)
            elif hasattr(agent_response, "text"):
                return str(agent_response.text)
            else:
                return str(agent_response)
    
    return "Workflow completed but no output extracted."

async def main():
    query = (
        "I am a learner, Cloud Engineer, employee ID EMP-001, targeting AZ-204. "
        "I need my full learning path, a study plan based on my workload "
        "(22 meeting hours per week, 10 focus hours, morning preference, 45% completion), "
        "and a readiness assessment."
    )

    client = FoundryChatClient(
        credential=AzureCliCredential(),
        project_endpoint=ENDPOINT,
        model=os.environ.get("AZURE_AI_AGENT_DEPLOYMENT_NAME", "gpt-4o"),
    )

    learning_path_curator = Agent(
        client=client,
        name="LearningPathCurator",
        instructions="""You are the Learning Path Curator for an enterprise certification system.
Given an employee role and target certification, retrieve and recommend:
1. Required certifications for the role
2. Recommended study hours  
3. Prerequisites
4. Logical study sequence
Only use knowledge from these roles: Cloud Engineer (AZ-204, AZ-305), 
DevOps Engineer (AZ-400, AZ-104), Data Engineer (DP-203, DP-900).
Pass your findings to the next agent."""
    )

    study_plan_generator = Agent(
        client=client,
        name="StudyPlanGenerator",
        instructions="""You are the Study Plan Generator for an enterprise certification system.
Given the learning path from the previous agent, create a capacity-aware study plan.
Risk thresholds: HIGH RISK: focus hours below 10, study hours below 15, practice score below 65.
MODERATE RISK: focus hours below 12, study hours below 20, practice score below 75.
Generate weekly schedule, milestone checkpoints, risk assessment.
Flag HIGH RISK employees for manager review.
Pass your findings to the next agent."""
    )

    assessment_agent = Agent(
        client=client,
        name="AssessmentAgent",
        instructions="""You are the Assessment Agent for an enterprise certification system.
Given the learning path and study plan, evaluate readiness:
1. Compare status against pass threshold (75%)
2. Generate 3-5 practice questions
3. Provide READY, MODERATE RISK, or HIGH RISK classification
4. Recommend next action
Never make promises about exam outcomes.
Pass your findings to the next agent."""
    )

    engagement_agent = Agent(
        client=client,
        name="EngagementAgent",
        instructions="""You are the Engagement Agent for an enterprise certification system.
Given all previous findings, create an engagement plan:
1. Suggest optimal study slots based on workload signals
2. Set reminders: HIGH RISK = daily, MODERATE RISK = every other day, LOW RISK = weekly
3. Synthesize all previous findings into a comprehensive final response
Add "Access logged for audit purposes" at the end.
End with "Is there anything else I can help you with?" """
    )

    workflow = SequentialBuilder(
        participants=[
            learning_path_curator,
            study_plan_generator,
            assessment_agent,
            engagement_agent,
        ]
    ).build()

    print("Running 4-agent sequential workflow...")
    print("=" * 60)

    run_result = await workflow.run(query, stream=True)

    async for event in run_result:
        if event.type == "executor_completed" and event.executor_id not in ["input-conversation", "complete"]:
            for item in event.data:
                if hasattr(item, "agent_response"):
                    resp = item.agent_response
                    text = ""
                    if hasattr(resp, "content"):
                        text = str(resp.content)
                    elif hasattr(resp, "text"):
                        text = str(resp.text)
                    if text:
                        print(f"\n[{event.executor_id}]:")
                        print(text[:800])
                        print("-" * 40)
        elif event.type == "output":
            agent_response = event.data
            text = ""
            if hasattr(agent_response, "content"):
                text = str(agent_response.content)
            elif hasattr(agent_response, "text"):
                text = str(agent_response.text)
            print(f"\nFINAL OUTPUT [{event.executor_id}]:")
            print(text)

if __name__ == "__main__":
    asyncio.run(main())