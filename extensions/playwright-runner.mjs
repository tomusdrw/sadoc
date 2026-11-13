// Execute from the repository/package directory after a locked dependency install:
// node "$SADOC_PLAYWRIGHT_RUNNER" [--network NAME] [--postgres-env NAME] -- COMMAND...
// The runtime is the policy-enforcing shim installed by sandbox.ts, never Docker.
import { spawnSync } from "node:child_process";
import { readFileSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { isAbsolute, join, relative } from "node:path";
import { parseArgs } from "node:util";

// Serialized into the container so no mount outside workspace/cache is needed.
async function preflight(packageName, expectedVersion, postgresEnv) {
    const require = (await import("node:module")).createRequire(process.cwd() + "/package.json");
    const actual = require(packageName + "/package.json").version;
    if (actual !== expectedVersion) throw Error(`Playwright changed: ${expectedVersion} -> ${actual}`);
    const playwright = require(packageName);
    for (const name of ["chromium", "firefox", "webkit"]) {
        const browser = await playwright[name].launch({
            headless: true, timeout: 30000,
            ...(name === "chromium" ? { chromiumSandbox: true } : {}),
        });
        try {
            const page = await browser.newPage();
            await page.setContent("<title>sadoc</title>", { timeout: 10000 });
            if (await page.title() !== "sadoc") throw Error(`${name} did not render`);
            console.log(`${name}: launch/render passed`);
        } finally {
            await browser.close();
        }
    }
    if (postgresEnv) {
        const { Client } = require("pg");
        const client = new Client({ connectionString: process.env[postgresEnv],
            connectionTimeoutMillis: 10000, query_timeout: 10000 });
        try {
            await client.connect();
            await client.query("SELECT 1");
            console.log("PostgreSQL: authenticated query passed");
        } finally {
            await client.end();
        }
    }
}

try {
    const { values, positionals } = parseArgs({
        options: { network: { type: "string" }, "postgres-env": { type: "string" } },
        allowPositionals: true,
    });
    if (process.env.SADOC_SANDBOX !== "1" || process.env.SADOC_CONTAINER_RUNTIME !== "podman") {
        throw Error("The Playwright runner requires Sadoc's sandbox with Podman enabled");
    }
    if (!positionals.length) throw Error("Supply the test command after --");
    const workspace = realpathSync(process.env.SADOC_WORKSPACE);
    const cache = realpathSync(process.env.SADOC_TOOL_CACHE);
    const cwd = realpathSync(process.cwd());
    const rel = relative(workspace, cwd);
    if (rel === ".." || rel.startsWith("../") || isAbsolute(rel)) {
        throw Error("Run from a package inside the disposable workspace");
    }
    if ([workspace, cache].some(path => path.includes(":"))) throw Error("Mount paths cannot contain ':'");
    const require = createRequire(join(cwd, "package.json"));
    let packageName;
    let version;
    for (const name of ["playwright", "@playwright/test"]) {
        let path;
        try { path = require.resolve(name + "/package.json"); }
        catch (error) { if (error.code === "MODULE_NOT_FOUND") continue; throw error; }
        const installed = JSON.parse(readFileSync(path, "utf8")).version;
        if (packageName && installed !== version) {
            throw Error("Installed playwright and @playwright/test have different versions; use a consistent locked install");
        }
        packageName ??= name;
        version = installed;
    }
    if (!packageName) throw Error("Install the project's locked Playwright dependencies first");
    if (typeof version !== "string" || !/^\d+\.\d+\.\d+$/.test(version)) {
        throw Error("A stable, exact installed Playwright version is required");
    }
    const postgresEnv = values["postgres-env"];
    if (postgresEnv && (!/^[A-Za-z_][A-Za-z_0-9]*$/.test(postgresEnv) || !process.env[postgresEnv])) {
        throw Error("--postgres-env must name a nonempty local test database variable");
    }
    const image = `mcr.microsoft.com/playwright:v${version}-noble`;
    const smoke = `(${preflight.toString()})(${JSON.stringify(packageName)}, ${JSON.stringify(version)}, ${JSON.stringify(postgresEnv ?? null)}).catch(e => { console.error(e); process.exitCode = 1; });`;
    const argv = ["run", "--rm", "--init", "--userns=keep-id", "--user", `${process.getuid()}:${process.getgid()}`,
        "--shm-size=1g", "--volume", `${workspace}:${workspace}:rw`,
        "--volume", `${cache}:${cache}:rw`, "--workdir", cwd,
        "--env", "HOME=/tmp/sadoc-playwright", "--env", "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
        "--env", `SADOC_TOOL_CACHE=${cache}`, "--env", `COREPACK_HOME=${cache}/corepack`,
        "--env", `npm_config_cache=${cache}/npm`];
    if (values.network) argv.push("--network", values.network);
    if (postgresEnv) argv.push("--env", `${postgresEnv}=${process.env[postgresEnv]}`);
    argv.push(image, "bash", "-e", "-o", "pipefail", "-c",
        'mkdir -p "$HOME"\nnode -e "$1"\nshift\nexec "$@"', "sadoc-playwright", smoke, ...positionals);
    console.log(`Playwright ${version}: preflight and tests inside ${image}`);
    const child = spawnSync("podman", argv, { stdio: "inherit" });
    if (child.error) throw child.error;
    process.exitCode = child.status ?? 1;
} catch (error) {
    console.error(error.message);
    process.exitCode = 1;
}
