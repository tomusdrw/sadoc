/**
 * The container helper, end to end.
 *
 * The runtime runs outside bubblewrap, reached through a shim that the sandbox
 * sees in place of the runtime itself. This checks that the round trip works:
 * the sandbox really does reach the runtime, and the policy is applied on the
 * way.
 */
import { buildSandbox, startContainerHelper, policyReason } from "./sandbox.ts";
import { spawn } from "node:child_process";

startContainerHelper();

/** Run a command in the sandbox and collect what it said. */
function inSandbox(command: string): Promise<{ code: number; output: string }> {
	return new Promise((resolve) => {
		const child = spawn("bash", ["-c", buildSandbox(command, process.cwd())], {
			env: process.env,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let output = "";
		child.stdout.on("data", (data) => {
			output += String(data);
		});
		child.stderr.on("data", (data) => {
			output += String(data);
		});
		child.on("close", (code) => resolve({ code: code ?? 1, output }));
	});
}

const checks: Array<{ name: string; ok: boolean; detail: string }> = [];

// The runtime answers through the shim.
const version = await inSandbox(`${process.env.SADOC_CONTAINER_RUNTIME || "podman"} --version`);
checks.push({
	name: "the runtime answers from inside the sandbox",
	ok: version.code === 0 && /version/i.test(version.output),
	detail: version.output.slice(0, 200),
});

// The policy is applied on the way out.
const privileged = await inSandbox(
	`${process.env.SADOC_CONTAINER_RUNTIME || "podman"} run --rm --privileged -v /:/host alpine echo NOPE`,
);
checks.push({
	name: "a privileged container is refused",
	ok: privileged.code !== 0 && /Blocked by sadoc policy/.test(privileged.output),
	detail: privileged.output.slice(0, 200),
});

// A mount from outside the workspace is refused.
const outside = await inSandbox(
	`${process.env.SADOC_CONTAINER_RUNTIME || "podman"} run --rm -v /etc:/etc alpine echo NOPE`,
);
checks.push({
	name: "a host directory outside the workspace is refused",
	ok: outside.code !== 0 && /Blocked by sadoc policy/.test(outside.output),
	detail: outside.output.slice(0, 200),
});

// Docker is never reachable: the policy refuses it, whatever the runtime is.
const docker = policyReason("docker --version", process.env.SADOC_CONTAINER_RUNTIME || "podman");
checks.push({
	name: "docker is refused",
	ok: docker !== null,
	detail: String(docker),
});

let bad = 0;
for (const check of checks) {
	if (!check.ok) {
		bad++;
		console.log(`  FAILED: ${check.name} - ${check.detail}`);
	}
}
console.log(`  ${checks.length - bad}/${checks.length} container helper checks pass`);
process.exit(bad === 0 ? 0 : 1);
