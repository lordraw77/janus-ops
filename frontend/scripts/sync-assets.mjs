// Copy brand assets from res/ (single source of truth) into public/ before dev/build.
import { copyFileSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const res = join(root, "..", "res");
const pub = join(root, "public");

mkdirSync(join(pub, "brand"), { recursive: true });

for (const file of readdirSync(join(res, "favicons"))) {
  copyFileSync(join(res, "favicons", file), join(pub, file));
}
copyFileSync(
  join(res, "logos", "janus-ops-logo-256.png"),
  join(pub, "brand", "janus-ops-logo-256.png"),
);

console.log("brand assets synced from res/");
