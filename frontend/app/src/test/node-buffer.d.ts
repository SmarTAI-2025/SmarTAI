// Test-only native byte readers for fake-indexeddb. jsdom's File lacks
// arrayBuffer(); these constructors exercise the byte paths used by browsers.
declare module "node:buffer" {
  export const Blob: typeof globalThis.Blob;
  export const File: typeof globalThis.File;
}
