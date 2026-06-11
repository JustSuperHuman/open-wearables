#!/usr/bin/env bun
/**
 * Deploy orchestrator for Open Wearables on Fly.io.
 *
 *   bun run deploy                 # interactive picker
 *   bun run deploy api frontend    # deploy specific components
 *   bun run deploy all             # deploy everything (in dependency order)
 *   bun run deploy --list          # show components and exit
 *
 * Each component maps a compose service onto its Fly app. See DEPLOY-FLY.md.
 */
import { $ } from "bun";

const ORG = "just-super-human";
const API_URL = "https://wearables.api.justgains.com";
const API_APP = "open-wearables-api";

type Component = {
  key: string;
  app: string;
  summary: string;
  /** Lower runs first (broker before consumers, api image before flower). */
  order: number;
  deploy: () => Promise<void>;
};

/** `fly apps create` is a no-op-ish if the app already exists — swallow that. */
async function ensureApp(app: string) {
  const existing = await $`fly apps list`.text().catch(() => "");
  if (existing.split(/\r?\n/).some((l) => l.split(/\s+/)[0] === app)) return;
  console.log(`  • creating app ${app} in ${ORG}`);
  await $`fly apps create ${app} --org ${ORG}`;
}

/** Current backend image ref, so Flower runs the exact same code as the API. */
async function backendImageRef(): Promise<string> {
  const out = await $`fly image show -a ${API_APP}`.text();
  const tag = out.match(/deployment-[A-Z0-9]+/)?.[0];
  if (!tag) throw new Error(`Could not resolve ${API_APP} image — deploy 'api' first.`);
  return `registry.fly.io/${API_APP}:${tag}`;
}

function randomHex(bytes = 16): string {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const COMPONENTS: Component[] = [
  {
    key: "redis",
    app: "open-wearables-redis",
    summary: "internal Redis broker / OAuth-state store (private 6PN only)",
    order: 0,
    async deploy() {
      await ensureApp(this.app);
      await $`fly deploy --config deploy/fly/redis.toml --image redis:8-alpine --ha=false`;
    },
  },
  {
    key: "api",
    app: API_APP,
    summary: `FastAPI + Celery worker/beat → ${API_URL}`,
    order: 1,
    async deploy() {
      await ensureApp(this.app);
      await $`fly deploy --config fly.toml --ha=false`.cwd("backend");
    },
  },
  {
    key: "flower",
    app: "open-wearables-flower",
    summary: "Celery dashboard (public, basic auth) — reuses the API image",
    order: 2,
    async deploy() {
      await ensureApp(this.app);
      const secrets = await $`fly secrets list -a ${this.app}`.text().catch(() => "");
      if (!secrets.includes("FLOWER_BASIC_AUTH")) {
        const pw = randomHex();
        console.log(`  • setting FLOWER_BASIC_AUTH = admin:${pw}  (save this!)`);
        await $`fly secrets set -a ${this.app} FLOWER_BASIC_AUTH=${`admin:${pw}`}`;
      }
      const image = await backendImageRef();
      await $`fly deploy --config deploy/fly/flower.toml --image ${image} --ha=false`;
    },
  },
  {
    key: "frontend",
    app: "open-wearables-frontend",
    summary: `Nitro web app (VITE_API_URL baked at build = ${API_URL})`,
    order: 3,
    async deploy() {
      await ensureApp(this.app);
      await $`fly deploy --config fly.toml --ha=false`.cwd("frontend");
    },
  },
  {
    key: "svix",
    app: "open-wearables-svix",
    summary: "Svix webhook server (internal). NEEDS secrets — see note below",
    order: 4,
    async deploy() {
      await ensureApp(this.app);
      const secrets = await $`fly secrets list -a ${this.app}`.text().catch(() => "");
      for (const req of ["SVIX_JWT_SECRET", "SVIX_DB_DSN", "SVIX_REDIS_DSN"]) {
        if (!secrets.includes(req)) {
          throw new Error(
            `${this.app} is missing secret ${req}. Set the Svix secrets first (see DEPLOY-FLY.md §4), then re-run.`,
          );
        }
      }
      await $`fly deploy --config deploy/fly/svix.toml --image svix/svix-server:v1 --ha=false`;
    },
  },
];

function resolve(tokens: string[]): Component[] {
  if (tokens.includes("all")) return [...COMPONENTS].sort((a, b) => a.order - b.order);
  const picked = new Map<string, Component>();
  for (const t of tokens) {
    const c =
      COMPONENTS.find((c) => c.key === t) ??
      COMPONENTS[Number(t) - 1]; // allow 1-based menu numbers
    if (!c) {
      console.error(`Unknown component: "${t}"`);
      process.exit(1);
    }
    picked.set(c.key, c);
  }
  return [...picked.values()].sort((a, b) => a.order - b.order);
}

function printList() {
  console.log("\nComponents:");
  COMPONENTS.forEach((c, i) =>
    console.log(`  ${i + 1}) ${c.key.padEnd(9)} ${c.app.padEnd(26)} ${c.summary}`),
  );
  console.log(`  *) all       deploy everything in dependency order\n`);
}

async function main() {
  let tokens = process.argv.slice(2);

  if (tokens.includes("--list") || tokens.includes("-l")) {
    printList();
    return;
  }

  if (tokens.length === 0) {
    printList();
    const answer = prompt("Deploy which? (names or numbers, comma/space separated, or 'all'):");
    if (!answer?.trim()) {
      console.log("Nothing selected.");
      return;
    }
    tokens = answer.split(/[\s,]+/).filter(Boolean);
  }

  const selected = resolve(tokens);
  console.log(`\nWill deploy: ${selected.map((c) => c.key).join(", ")}\n`);

  const failed: string[] = [];
  for (const c of selected) {
    console.log(`\n── deploying ${c.key} (${c.app}) ─────────────────────────────`);
    try {
      await c.deploy();
      console.log(`✓ ${c.key} deployed`);
    } catch (err) {
      console.error(`✗ ${c.key} failed: ${err instanceof Error ? err.message : err}`);
      failed.push(c.key);
    }
  }

  console.log("\n────────────────────────────────");
  console.log(`Done. ${selected.length - failed.length}/${selected.length} succeeded.`);
  if (failed.length) {
    console.log(`Failed: ${failed.join(", ")}`);
    process.exit(1);
  }
}

main();
