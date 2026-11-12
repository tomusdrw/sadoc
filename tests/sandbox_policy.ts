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
import { policyReason } from "./sandbox.ts";

// [command, allowedRuntime, shouldBlock]
const cases: Array<[string, string | null, boolean]> = [
	// The allowed runtime, used properly.
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
process.exit(bad === 0 ? 0 : 1);
