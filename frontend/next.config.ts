import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The container runs `node server.js` from .next/standalone; see frontend/Dockerfile.
  // Vercel supplies its own server runtime and standalone output is only a self-hosting
  // packaging mode, so it is disabled there.
  output: process.env.VERCEL ? undefined : "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
