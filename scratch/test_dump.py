import asyncio
import sys
import os
import traceback

sys.path.insert(0, os.path.abspath('backend'))

from core.planner.planner_agent import PlannerAgent
import logging

logging.basicConfig(level=logging.DEBUG)

async def dump_tasks():
    await asyncio.sleep(5)
    print("\n--- DUMPING TASKS ---")
    for task in asyncio.all_tasks():
        print(f"Task: {task.get_name()}")
        task.print_stack()
    print("---------------------\n")
    sys.exit(1)

async def main():
    asyncio.create_task(dump_tasks())
    
    agent = PlannerAgent()
    print("Agent created")
    
    # We will step into get_chain
    print("Calling planner create_plan")
    
    try:
        plan = await agent.create_plan("show me data distribution", "This is some dummy data.")
        print("Plan:", plan)
    except Exception as e:
        print("Exception:", e)

if __name__ == "__main__":
    asyncio.run(main())