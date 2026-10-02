import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const adminBuild = mode === "admin";
  return {
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
    build: {
      rollupOptions: {
        input: path.resolve(__dirname, adminBuild ? "admin.html" : "index.html"),
      },
      outDir: adminBuild ? "dist-admin" : "dist",
    },
  };
});

