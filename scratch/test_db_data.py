import os
import sys
from supabase import create_client
from dotenv import load_dotenv

load_dotenv('e:/Agentloop/backend/.env')

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

client = create_client(url, key)

print("Fetching agent_runs...")
try:
    res = client.table("agent_runs").select("id, user_id, workspace_id, created_at, query").execute()
    runs = res.data
    print(f"Total runs: {len(runs)}")
    for r in runs:
        print(r)
except Exception as e:
    print("Error:", e)
