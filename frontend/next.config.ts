import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The container runs `node server.js` from .next/standalone; see frontend/Dockerfile.
  output: "standalone",
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
