import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, normalizePath } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const adminBuild = mode === "admin";
  return {
  plugins: [react(), tailwindcss(), {
    name: "public-workspace-boundary",
    generateBundle(_options, bundle) {
      if (adminBuild) return;
      for (const output of Object.values(bundle)) {
        if (output.type !== "chunk") continue;
        for (const moduleId of Object.keys(output.modules)) {
          if (/\/src\/(?:routes\/(?:admin|teacher|student)\/|api\/(?:hooks\/)?education\.ts|data\/frontierDemo\.ts|test\/fixtures\/)/.test(normalizePath(moduleId))) {
            this.error(`Retired/private workspace entered the public bundle: ${moduleId}`);
          }
        }
      }
    },
  }, viteStaticCopy({
    targets: [...["wasm", "cmaps", "standard_fonts", "iccs"].map((directory) => ({
      src: normalizePath(path.resolve(__dirname, `node_modules/pdfjs-dist/${directory}/*`)),
      dest: `pdfjs/${directory}`,
      rename: { stripBase: true },
    })), ...(mode === "e2e" ? [{
      src: normalizePath(path.resolve(__dirname, "e2e/fixtures/*.pdf")),
      dest: "e2e/fixtures",
      rename: { stripBase: true },
    }] : [])],
  })],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
    build: {
      rollupOptions: {
        input: mode === "e2e" ? {
          app: path.resolve(__dirname, "index.html"),
          pdfPreview: path.resolve(__dirname, "e2e/fixtures/pdf-preview.html"),
        } : path.resolve(__dirname, adminBuild ? "admin.html" : "index.html"),
      },
      outDir: adminBuild ? "dist-admin" : "dist",
    },
  };
});
