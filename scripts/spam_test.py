import json, time, sys, base64
from curl_cffi import requests

with open('results/batch_20260809_050511_d05a62/accounts.json') as f:
    accounts = json.load(f)
token = accounts[0]['tokens']['access_token']
email = accounts[0]['email']

part = token.split('.')[1]
part += '=' * (-len(part) % 4)
claims = json.loads(base64.urlsafe_b64decode(part))

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}",
    "User-Agent": "xai-grok-cli/1.0.0",
    "x-grok-client-version": "1.0.0",
    "x-grok-client-surface": "grok-build",
}

prompts = [
    "What is 2+2?", "Name 3 colors", "What is the capital of France?",
    "Write a haiku about rain", "List 5 programming languages", "What does CPU stand for?",
    "Name a fruit starting with A", "What is 10 times 10?", "Explain gravity in one sentence",
    "Name 3 JavaScript frameworks", "Boiling point of water?", "Write a function to add two numbers in Python",
    "What does HTTP stand for?", "Name 3 database types", "Largest planet?",
    "Explain recursion in one sentence", "Name 3 cloud providers", "What does API stand for?",
    "List 3 sorting algorithms", "Speed of light?", "Name 3 Python web frameworks",
    "What does DNS do?", "Write a SQL SELECT", "Name 3 version control systems",
    "What is a closure?", "Explain Big O briefly", "Name 3 NoSQL databases",
    "What does SSD stand for?", "List 3 Linux distros", "What is a primary key?",
    "Name 3 design patterns", "What does JSON stand for?", "Explain what a thread is",
    "Name 3 CI/CD tools", "What is memoization?", "Name 3 message queues",
    "What does REST stand for?", "Explain polymorphism briefly", "Name 3 container tools",
    "What is a hash table?", "Name 3 monitoring tools", "What does CRUD stand for?",
    "Explain microservices briefly", "Name 3 load balancers", "What is a deadlock?",
    "Name 3 search engines", "What does ORM stand for?", "Explain ACID briefly",
    "Name 3 cache strategies", "Binary search time complexity?", "Name 3 DevOps tools",
    "What is a socket?", "Name 3 API protocols", "What does VM stand for?",
    "Explain what a daemon is", "Name 3 package managers", "What is a context switch?",
    "Name 3 firewalls", "What does IP mean?", "Explain what a port is",
    "Name 3 proxy servers", "What is a CDN?", "Name 3 SSL/TLS versions",
    "What does SSH stand for?", "Explain what a VPN is", "Name 3 web servers",
    "What is a cookie?", "Name 3 authentication methods", "What does URL stand for?",
    "Explain what a domain is", "Name 3 email protocols", "What is a subnet?",
    "Name 3 routing protocols", "What does LAN stand for?", "Explain what NAT is",
    "Name 3 DNS record types", "What is a PTR record?", "Name 3 Linux shells",
    "What does AWK do?", "Explain what grep is", "Name 3 text editors",
    "What is a kernel?", "Name 3 filesystems", "What does inode mean?",
    "Explain what a symlink is", "Name 3 archive formats", "What is a checksum?",
    "Name 3 compression algorithms", "What does tar stand for?", "Explain what gzip is",
    "Name 3 binary formats", "What is a hex dump?", "Name 3 debuggers",
    "What does GDB do?", "Explain what strace is", "Name 3 profiling tools",
    "What is a core dump?", "Name 3 compilers",
]

total_tokens = 0
request_count = 0
think_lost = False
bfs_flagged = False
rate_limited = False
start_time = time.time()

with open('/opt/grok-farm/results/spam_test_results.txt', 'w') as logf:
    logf.write(f"Account: {email}\n")
    logf.write(f"JWT bfs: {claims.get('bfs', 'not set')}\n")
    logf.write(f"Starting spam test at {time.strftime('%H:%M:%S')}\n")
    logf.write(f"{'='*60}\n")
    
    for i, prompt in enumerate(prompts):
        try:
            resp = requests.post(
                "https://cli-chat-proxy.grok.com/v1/responses",
                json={"model": "grok-4.5", "input": prompt, "max_output": 500},
                headers=headers,
                impersonate="chrome131",
                timeout=90,
            )
        except Exception as e:
            line = f"[{i+1:3d}] ERROR: {str(e)[:80]}"
            print(line)
            logf.write(line + "\n")
            logf.flush()
            time.sleep(3)
            continue
        
        status = resp.status_code
        request_count += 1
        
        if status == 200:
            data = resp.json()
            usage = data.get("usage", {})
            tot = usage.get("total_tokens", 0)
            rt = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
            total_tokens += tot
            has_think = rt > 0
            
            if not has_think and not think_lost:
                think_lost = True
                line = f"\n{'!'*50}\nTHINKING LOST at request {i+1}!\n{'!'*50}\n"
                print(line)
                logf.write(line + "\n")
            
            marker = "Y" if has_think else "N"
            line = f"[{i+1:3d}] 200 total={tot:5d} think={rt:4d} [{marker}]"
            print(line)
            logf.write(line + "\n")
            logf.flush()
            
        elif status == 403:
            bfs_flagged = True
            line = f"\n[{i+1:3d}] 403 BFS BLOCKED — {resp.text[:200]}"
            print(line)
            logf.write(line + "\n")
            logf.flush()
            break
        elif status == 429:
            rate_limited = True
            line = f"\n[{i+1:3d}] 429 RATE LIMITED — {resp.text[:200]}"
            print(line)
            logf.write(line + "\n")
            logf.flush()
            break
        elif status == 202:
            think_lost = True
            line = f"[{i+1:3d}] 202 NOTHINK"
            print(line)
            logf.write(line + "\n")
            logf.flush()
        else:
            line = f"[{i+1:3d}] {status} — {resp.text[:150]}"
            print(line)
            logf.write(line + "\n")
            logf.flush()
        
        time.sleep(1.5)
    
    elapsed = time.time() - start_time
    summary = f"""
{'='*60}
FINAL SUMMARY for {email}
  Total requests:      {request_count}
  Total tokens:       {total_tokens:,}
  Elapsed:            {elapsed:.0f}s
  Thinking lost:       {think_lost}
  BFS flagged:         {bfs_flagged}
  Rate limited:        {rate_limited}
{'='*60}
"""
    print(summary)
    logf.write(summary + "\n")
    logf.flush()
