interface ImportMetaEnv {
  readonly VITE_SMARTAI_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "*?url" {
  const assetUrl: string;
  export default assetUrl;
}
