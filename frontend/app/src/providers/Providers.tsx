import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Toaster } from "sonner";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ThemeProvider } from "@/theme/ThemeProvider";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <ThemeProvider>
          {children}
          {/* Notices stay below modal navigation (50) and leave confirmations (80). */}
          <Toaster richColors position="top-right" mobileOffset={{ top: 86 }} style={{ zIndex: 40 }} />
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>
  );
}

