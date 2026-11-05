#include <stdio.h>
int main(void) {
    int n; long long x, total = 0;
    if (scanf("%d", &n) != 1) return 1;
    for (int i = 0; i < n; ++i) {
        if (scanf("%lld", &x) != 1) return 1;
        total += (x < 0 ? -x : x);
    }
    printf("%lld\n", total);
    return 0;
}
