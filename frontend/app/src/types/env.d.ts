interface ImportMetaEnv {
  readonly VITE_SMARTAI_BACKEND_URL?: string;
  readonly VITE_SMARTAI_PROMO_YOUTUBE_ZH_URL?: string;
  readonly VITE_SMARTAI_PROMO_YOUTUBE_EN_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "*?url" {
  const assetUrl: string;
  export default assetUrl;
}
