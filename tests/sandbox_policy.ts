/**
 * The sandbox policy, checked against the real extension.
 *
 * The Python module `sadoc.policy` owns the rules and hands them over in the
 * environment. This file runs the extension against those rules, so the two
 * cannot drift apart without the tests noticing.
 *
 * Run it with bun:
 *   SADOC_CONTAINER_POLICY='{"container": [...], "runtimes": [...]}' bun run run.ts
 */
import sandbox, { buildSandbox, policyReason, containerReason } from "./sandbox.ts";

import { spawnSync } from "node:child_process";
import assert from "node:assert/strict";

const WORKSPACE = process.env.SADOC_WORKSPACE || process.cwd();

// [command, allowedRuntime, shouldBlock]
const cases: Array<[string, string | null, boolean]> = [
	// The allowed runtime, used properly.
    ["podman run --rm docker.io/library/postgres:17-alpine", "podman", false],
	["podman run --rm -v .:/w -w /w node:22 npm test", "podman", false],
	['podman run --rm -v "$PWD:/w" -w /w postgres:17 pg_ctl start', "podman", false],
	// The allowed runtime, escaping.
	["podman run --privileged -v /:/host alpine", "podman", true],
	["podman run --network=host alpine", "podman", true],
	["podman run --net host alpine", "podman", true],
	["podman run --pid=host alpine", "podman", true],
	["podman run --ipc=host alpine", "podman", true],
	["podman run --uts=host alpine", "podman", true],
	["podman run --userns=host alpine", "podman", true],
	["podman run --cap-add=SYS_ADMIN alpine", "podman", true],
	["podman run --cap-add ALL alpine", "podman", true],
	["podman run --security-opt seccomp=unconfined alpine", "podman", true],
	["podman run --device /dev/sda alpine", "podman", true],
	["podman run -v /var/run/docker.sock:/x alpine", "podman", true],
	["podman run -v /run/user/1000/podman/podman.sock:/x alpine", "podman", true],
	["podman run -v /etc:/etc alpine", "podman", true],
	["podman run -v /:/host alpine", "podman", true],
	["podman run --mount source=/etc,target=/etc alpine", "podman", true],
	// Docker is never allowed, even when it is the runtime that was asked for.
	["docker run alpine", null, true],
	["docker run alpine", "podman", true],
	["docker run alpine", "docker", true],
	// With containers off, the runtime itself is refused.
	["podman run alpine", null, true],
	// A container may have the workspace and the cache, and nothing else.
	["podman run -v .:/w -w /w node:22 npm test", "podman", false],
	['podman run -v "$PWD:/w" -w /w node:22 npm test', "podman", false],
	["podman run -v /etc:/etc alpine", "podman", true],
	["podman run -v /home/someone/.ssh:/keys alpine", "podman", true],
	["podman run --mount type=bind,source=/etc,target=/etc alpine", "podman", true],
	[`podman run --mount type=bind,source=${WORKSPACE}/sub,target=/w alpine`, "podman", false],
	// The shell rules still hold.
	["git push origin main", "podman", true],
	["gh pr create --title x", "podman", true],
	["sudo pacman -S podman", "podman", true],
	["systemctl restart x", "podman", true],
	["pnpm install --frozen-lockfile", "podman", false],
	["pnpm test", null, false],
];

let bad = 0;
for (const [cmd, runtime, shouldBlock] of cases) {
	const reason = policyReason(cmd, runtime);
	const blocked = reason !== null;
	if (blocked !== shouldBlock) {
		bad++;
		console.log(`  MISMATCH: ${cmd} (runtime=${runtime}) -> ${reason ?? "allowed"}`);
	}
}
console.log(`  ${cases.length - bad}/${cases.length} policy cases behave as expected`);

// The helper runs the runtime outside the sandbox, so it applies the
// container rules on its own. These are the same rules, checked against the
// argument list the helper receives.
const containerCases: Array<[string, boolean]> = [
    ["run --label io.sadoc.session=other example.invalid/image", true],
    ["run --label-file labels example.invalid/image", true],
	["run --rm -v .:/w -w /w node:22 npm test", false],
	["build -t thing .", false],
	["run --rm --privileged -v /:/host alpine", true],
	["run --rm --network=host alpine", true],
	["run --rm --pid=host alpine", true],
	["run --rm --cap-add=SYS_ADMIN alpine", true],
	["run --rm --device /dev/sda alpine", true],
	["run --rm -v /etc:/etc alpine", true],
	["run --rm -v /:/host alpine", true],
	["run --rm --mount source=/etc,target=/etc alpine", true],
];
let containerBad = 0;
for (const [argv, shouldBlock] of containerCases) {
	const reason = containerReason(argv);
	const blocked = reason !== null;
	if (blocked !== shouldBlock) {
		containerBad++;
		console.log(`  MISMATCH: container ${argv} -> ${reason ?? "allowed"}`);
	}
}
console.log(
	`  ${containerCases.length - containerBad}/${containerCases.length} container cases behave as expected`,
);
// Exercise real command execution: a hung subprocess must not reach the marker.
const timed = spawnSync("bash", ["-c", buildSandbox("sleep 5; echo escaped-timeout", WORKSPACE, 0.1)],
    {encoding: "utf8", timeout: 3000});
assert.equal(timed.status, 124, timed.stderr);
assert.ok(!timed.stdout.includes("escaped-timeout"));

const handlers: Record<string, Function> = {};
sandbox({on(name: string, handler: Function) { handlers[name] = handler; }} as any);
for (let i = 0; i < 2; i++) {
    const event = {toolName: "bash", toolCallId: `failed-${i}`, input: {command: "make test", timeout: 99999}};
    assert.equal(await handlers.tool_call(event), undefined);
    assert.equal(event.input.timeout, 900);
    await handlers.tool_execution_end({toolCallId: event.toolCallId, toolName: "bash", isError: true});
}
assert.equal((await handlers.tool_call({toolName: "bash", toolCallId: "third", input: {command: "make test"}})).block, true);
for (let i = 0; i < 4; i++) {
    const event = {toolName: "bash", toolCallId: `ok-${i}`, input: {command: "make build", timeout: 10}};
    assert.equal(await handlers.tool_call(event), undefined);
    assert.equal(event.input.timeout, 10);
    await handlers.tool_execution_end({toolCallId: event.toolCallId, toolName: "bash", isError: false});
}
for (let i = 0; i < 2; i++) {
    assert.equal(await handlers.tool_call({toolName: "bash", toolCallId: `parallel-${i}`, input: {command: "make parallel"}}), undefined);
}
assert.equal((await handlers.tool_call({toolName: "bash", toolCallId: "parallel-third", input: {command: "make parallel"}})).block, true);
process.exit(bad === 0 && containerBad === 0 ? 0 : 1);
