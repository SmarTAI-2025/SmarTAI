#include <iostream>
int main() {
    int n; long long x, total = 0;
    if (!(std::cin >> n)) return 1;
    for (int i = 0; i < n; ++i) {
        if (!(std::cin >> x)) return 1;
        total += (x < 0 ? -x : x);
    }
    std::cout << total << '\n';
    return 0;
}
