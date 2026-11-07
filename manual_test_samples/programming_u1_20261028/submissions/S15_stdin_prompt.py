import sys
values = list(map(int, sys.stdin.read().split()))
n = values[0]
print("sum =", sum(values[1:1+n]))
