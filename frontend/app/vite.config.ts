import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, normalizePath } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const adminBuild = mode === "admin";
  return {
  plugins: [react(), tailwindcss(), viteStaticCopy({
    targets: ["wasm", "cmaps", "standard_fonts", "iccs"].map((directory) => ({
      src: normalizePath(path.resolve(__dirname, `node_modules/pdfjs-dist/${directory}/*`)),
      dest: `pdfjs/${directory}`,
      rename: { stripBase: true },
    })),
  })],
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
