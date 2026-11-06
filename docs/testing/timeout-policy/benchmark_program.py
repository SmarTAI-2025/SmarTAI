import asyncio, json, platform, time
from backend.tools.code_interpreter import run_python_subprocess

PROGRAMS = {
 'sum_one_million': 'print(sum(range(1000000)))',
 'sort_200000': 'import random\nr=random.Random(42)\na=[r.randrange(1000000) for _ in range(200000)]\na.sort()\nprint(len(a))',
 'sieve_two_million': 'n=2000000\na=bytearray(b"\\x01")*(n+1)\na[:2]=b"\\x00\\x00"\nfor i in range(2,int(n**.5)+1):\n if a[i]: a[i*i:n+1:i]=b"\\x00"*((n-i*i)//i+1)\nprint(sum(a))',
 'dp_one_million_cells': 'a=[0]*1001\nfor i in range(1000):\n for j in range(1,1001): a[j]=(a[j]+a[j-1]+1)%1000000007\nprint(a[-1])',
}
async def main():
 out={'host':platform.platform(),'python':platform.python_version(),'runner':'local host; no OCI startup, no cloud contention','samples':{}}
 for name,code in PROGRAMS.items():
  durations=[]
  for _ in range(3):
   start=time.perf_counter(); result=await run_python_subprocess(code,timeout=5)
   assert not result.error, result
   durations.append(round(time.perf_counter()-start,4))
  out['samples'][name]=durations
 print(json.dumps(out,indent=2))
asyncio.run(main())
