/**
 * deps-bot sandbox + policy extension for pi.
 *
 * Loaded with:  pi --no-extensions -e extensions/sandbox.ts
 *
 * Guarantees:
 *  1. Every shell command runs inside bubblewrap:
 *       - no network (--unshare-net) unless DEPSBOT_ALLOW_NET=1
 *       - the workspace is read-write, the rest of / is read-only
 *       - /tmp and tool caches are writable (HOME is redirected to a scratch dir)
 *  2. The agent can never mutate remote state. git push / gh pr create / gh api
 *     writes / sudo / systemctl are blocked outright - the orchestrator owns those.
 *  3. File writes are restricted to the workspace directory.
 *
 * The pi process itself keeps network access (it must reach the LLM server);
 * only the agent's shell commands are network-isolated.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const WORKSPACE = process.env.DEPSBOT_WORKSPACE || process.cwd();
const ALLOW_NET = process.env.DEPSBOT_ALLOW_NET === "1";
// A persistent, writable directory for the package managers' caches. Without
// it every run re-downloads the whole registry, which is slow and flaky.
const TOOL_CACHE = process.env.DEPSBOT_CACHE_DIR || "/tmp/depsbot-toolcache";
const SHELL_TOOLS = new Set(["bash", "shell"]);

const FORBIDDEN: Array<[RegExp, string]> = [
	[/\bgit\s+push\b/i, "git push"],
	[/\bgit\s+merge\b/i, "git merge"],
	[/\bgit\s+rebase\b/i, "git rebase"],
	[/\bgit\s+reset\s+--hard\b/i, "git reset --hard"],
	[/\bgit\s+clean\b/i, "git clean"],
	[/\bgit\s+config\b.*\b(user\.|credential\.)/i, "git config user/credential"],
	[/\bgh\s+pr\s+(create|merge|close|edit|ready|review|delete)\b/i, "gh pr write"],
	[/\bgh\s+(issue|release|gist)\s+(create|edit|close|delete)\b/i, "gh write"],
	[/\bgh\s+api\b[^\n]*\b(POST|PUT|PATCH|DELETE)\b/i, "gh api write"],
	[/\bgh\s+auth\s+(login|logout|refresh)\b/i, "gh auth"],
	[/\bsudo\b/i, "sudo"],
	[/\bsystemctl\b/i, "systemctl"],
	[/\bdocker\b/i, "docker"],
	[/\bpodman\b/i, "podman"],
	[/\bcurl\b[^|;&]*\|\s*(ba)?sh/i, "curl | sh"],
	[/\beval\b/i, "eval"],
	[/\bnc\b[^|;&]*\s-l/i, "netcat listener"],
];

function q(s: string): string {
	return "'" + s.replace(/'/g, `'\\''`) + "'";
}

/** Build the bubblewrap argv for a single command. */
export function buildSandbox(command: string, cwd: string): string {
	const scratch = "/tmp/depsbot-scratch";
	const cache = TOOL_CACHE;
	const realHome = process.env.DEPSBOT_REAL_HOME || "/root";
	// NOTE: /bin/sh is bash in POSIX mode on this box, where sourcing a missing
	// file aborts the whole script. Use bash explicitly and guard every source.
	const inner = [
		"set +e",
		"[ -r /etc/profile ] && . /etc/profile",
		`[ -r ${q(realHome)}/.profile ] && . ${q(realHome)}/.profile`,
		`export HOME=${q(scratch)}`,
		`export XDG_CACHE_HOME=${q(scratch)}/.cache`,
		// The package managers' caches live in the persistent cache directory,
		// not in the scratch tmpfs, so a run does not re-download the registry.
		`export CARGO_HOME=${q(cache)}/cargo`,
		`export npm_config_cache=${q(cache)}/npm`,
		`export GOCACHE=${q(cache)}/go-build`,
		`export GOPATH=${q(cache)}/go`,
		`export GOMODCACHE=${q(cache)}/go/pkg/mod`,
		`export PIP_CACHE_DIR=${q(cache)}/pip`,
		`export UV_CACHE_DIR=${q(cache)}/uv`,
		`export BUN_INSTALL_CACHE_DIR=${q(cache)}/bun`,
		command,
	].join("\n");
	const argv = [
		"bwrap",
		...(ALLOW_NET ? [] : ["--unshare-net"]),
		"--unshare-pid",
		"--unshare-ipc",
		"--unshare-uts",
		"--die-with-parent",
		"--ro-bind / /",
		// --dev /dev must come *after* --ro-bind / /, otherwise the read-only
		// bind of / shadows it and /dev/null is not writable.
		"--dev /dev",
		"--proc /proc",
		// Mount order matters: --tmpfs /tmp replaces /tmp entirely, so the
		// workspace bind has to come *after* it or it is shadowed away.
		"--tmpfs /tmp",
		`--dir ${q(scratch)}`,
		`--dir ${q(cache)}`,
		`--bind ${q(WORKSPACE)} ${q(WORKSPACE)}`,
		`--bind ${q(cache)} ${q(cache)}`,
		// Explicit chdir: without it bubblewrap may fall back to $HOME, which
		// would put the agent in the scratch dir and lose repo writes.
		`--chdir ${q(WORKSPACE)}`,
		"--setenv HOME /tmp/depsbot-scratch",
		"--setenv TMPDIR /tmp",
		"--setenv DEPSBOT_SANDBOX 1",
		// NOTE: the inner script must be quoted, otherwise the *outer* shell that
		// pi spawns splits it on ";" / "||" and runs parts outside the sandbox.
		"bash", "-c", q(inner),
	];
	return argv.join(" ");
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event: any) => {
		if (SHELL_TOOLS.has(event.toolName)) {
			const command = String(event.input.command ?? "");
			for (const [pattern, label] of FORBIDDEN) {
				if (pattern.test(command)) {
					return {
						block: true,
						reason:
							`Blocked by deps-bot policy: "${label}" is not allowed in the agent sandbox. ` +
							`The orchestrator owns all remote state (push, PR create, PR merge).`,
					};
				}
			}
			// Mutate in place: pi executes the (mutated) command.
			event.input.command = buildSandbox(command, String(event.input.cwd ?? WORKSPACE));
			return undefined;
		}

		if (event.toolName === "write" || event.toolName === "edit") {
			const path = String(event.input.path ?? "");
			const abs = path.startsWith("/") ? path : `${WORKSPACE}/${path}`;
			if (!abs.startsWith(WORKSPACE)) {
				return { block: true, reason: `Writes are restricted to the workspace (${WORKSPACE}).` };
			}
		}
		return undefined;
	});
}
