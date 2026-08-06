#!/usr/bin/env python3
"""Auto-bind unbound grok_build accounts to WARP egress nodes (round-robin)."""
import json,urllib.request,os,sys
B=os.environ.get("GROK2API_URL","http://127.0.0.1:8000")
def req(method,p,b=None,t=None):
    h={"Content-Type":"application/json"}
    if t: h["Authorization"]="Bearer "+t
    data=json.dumps(b).encode() if b else None
    r=urllib.request.Request(B+p,data=data,headers=h,method=method)
    try: return json.load(urllib.request.urlopen(r,timeout=30))
    except urllib.error.HTTPError as e: return json.loads(e.read())
# login
t=req("POST","/api/admin/v1/auth/login",{"username":os.environ["GROK2API_ADMIN_USER"],"password":os.environ["GROK2API_ADMIN_PASS"]})["data"]["tokens"]["accessToken"]
# find healthy grok_build egress nodes
nodes=[]
d=req("GET","/api/admin/v1/egress-nodes",None,t)
for n in d.get("data",{}).get("items",[]):
    if n.get("scope")=="grok_build" and n.get("enabled"):
        nodes.append(str(n.get("id")))
if len(nodes)<2:
    print("need 2+ egress nodes, found: "+str(len(nodes))); sys.exit(0)
# scan for unbound enabled grok_build accounts
unbound=[]
total=req("GET","/api/admin/v1/accounts?pageSize=1",None,t)["data"]["total"]
ps=100;pages=(total//ps)+1
for pg in range(1,pages+1):
    d2=req("GET","/api/admin/v1/accounts?page="+str(pg)+"&pageSize="+str(ps),None,t)
    for a in d2.get("data",{}).get("items",[]):
        if a.get("enabled") and a.get("provider")=="grok_build" and not a.get("egressNodeId"):
            unbound.append(str(a.get("id")))
if not unbound:
    print("no unbound accounts"); sys.exit(0)
print("unbound: "+str(len(unbound)))
# assign round-robin
for i,aid in enumerate(unbound):
    nid=nodes[i%len(nodes)]
    r=req("POST","/api/admin/v1/egress-nodes/"+nid+"/accounts",{"provider":"grok_build","ids":[aid]},t)
    if "error" not in r: print("  "+aid+" -> node "+nid)
print("done")
