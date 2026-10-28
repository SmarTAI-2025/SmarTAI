def fib(n):
    if n == 0:
        return 1 // n
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
