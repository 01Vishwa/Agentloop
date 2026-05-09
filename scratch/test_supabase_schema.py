import os
import sys
from supabase import create_client

# setup from .env
from dotenv import load_dotenv
load_dotenv('e:/Agentloop/backend/.env')

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

client = create_client(url, key)

print("Testing agent_runs...")
try:
    res = client.table("agent_runs").select("*").limit(1).execute()
    print("agent_runs schema:", res.data[0].keys() if res.data else "empty")
except Exception as e:
    print("Error on agent_runs:", e)

print("Testing uploaded_files...")
try:
    res = client.table("uploaded_files").select("*").limit(1).execute()
    print("uploaded_files schema:", res.data[0].keys() if res.data else "empty")
except Exception as e:
    print("Error on uploaded_files:", e)
