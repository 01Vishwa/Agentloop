import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath('backend'))

from core.planner.planner_agent import PlannerAgent

async def main():
    agent = PlannerAgent()
    print("Agent created")
    plan = await agent.create_plan("show me data distribution", "This is some dummy data.")
    print("Plan:", plan)

if __name__ == "__main__":
    asyncio.run(main())