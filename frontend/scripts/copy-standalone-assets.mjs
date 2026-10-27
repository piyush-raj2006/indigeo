// The runtime stage of frontend/Dockerfile copies public/ and .next/static into the
// standalone bundle. A local `npm start` needs the same layout: .next/standalone/server.js
// does not read the project root, so without this copy every /_next/static request 404s.
import { cpSync, existsSync } from "node:fs";

for (const [from, to] of [
  ["public", ".next/standalone/public"],
  [".next/static", ".next/standalone/.next/static"],
]) {
  if (existsSync(from)) cpSync(from, to, { recursive: true });
}
