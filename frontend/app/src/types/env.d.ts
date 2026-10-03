interface ImportMetaEnv {
  readonly MODE: string;
  readonly BASE_URL: string;
  readonly VITE_SMARTAI_BACKEND_URL?: string;
  readonly VITE_SMARTAI_ADMIN_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "*?url" {
  const assetUrl: string;
  export default assetUrl;
}
