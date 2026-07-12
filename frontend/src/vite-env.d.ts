/// <reference types="vite/client" />

// Typed access to the build-time environment variables the console reads.
// Keeping this interface explicit means `import.meta.env` is never `any`.
interface ImportMetaEnv {
  /** Origin of the Gozar backend API (e.g. "http://localhost:8000"). Empty = same origin. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
