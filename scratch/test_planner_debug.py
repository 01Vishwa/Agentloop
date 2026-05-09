import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath('backend'))

from core.planner.planner_agent import PlannerAgent
import logging

logging.basicConfig(level=logging.DEBUG)

async def main():
    agent = PlannerAgent()
    print("Agent created")
    
    # We will step into get_chain
    print("Calling planner create_plan")
    
    try:
        plan = await asyncio.wait_for(agent.create_plan("show me data distribution", "This is some dummy data."), timeout=20.0)
        print("Plan:", plan)
    except asyncio.TimeoutError:
        print("TIMED OUT!")

if __name__ == "__main__":
    asyncio.run(main())