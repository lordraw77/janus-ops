import type { NextConfig } from "next";

// Production: static export served by the FastAPI app (ADR-009, single image).
// Development: `next dev` proxies /api to a locally running backend.
const isDev = process.env.NODE_ENV === "development";
const devApiOrigin = process.env.JANUS_DEV_API_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  trailingSlash: true, // /inventory -> /inventory/index.html, served as-is by StaticFiles
  skipTrailingSlashRedirect: true, // keep /api/v1/... untouched in dev
  images: { unoptimized: true },
  ...(isDev
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: `${devApiOrigin}/api/:path*` }];
        },
      }
    : { output: "export" as const }),
};

export default nextConfig;
