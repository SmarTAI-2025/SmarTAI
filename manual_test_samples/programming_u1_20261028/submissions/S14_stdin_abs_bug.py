import sys
values = list(map(int, sys.stdin.read().split()))
n = values[0]
print(sum(abs(x) for x in values[1:1+n]))
