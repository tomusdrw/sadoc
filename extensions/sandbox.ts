/**
 * sadoc sandbox + policy extension for pi.
 *
 * Loaded with:  pi --no-extensions -e extensions/sandbox.ts
 *
 * Guarantees:
 *  1. Every shell command runs inside bubblewrap:
 *       - no network (--unshare-net) unless SADOC_ALLOW_NET=1
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
import { createLocalBashOperations } from "@earendil-works/pi-coding-agent";

const WORKSPACE = process.env.SADOC_WORKSPACE || process.cwd();
const ALLOW_NET = process.env.SADOC_ALLOW_NET === "1";
// A persistent, writable directory for the package managers' caches. Without
// it every run re-downloads the whole registry, which is slow and flaky.
const TOOL_CACHE = process.env.SADOC_CACHE_DIR || "/tmp/depsbot-toolcache";
// Rootless containers, for test environments that would otherwise be installed
// onto the machine. Off by default.
const ALLOW_CONTAINERS = process.env.SADOC_ALLOW_CONTAINERS === "1";
const RUNTIME = process.env.SADOC_CONTAINER_RUNTIME || "podman";
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
	[/\bcurl\b[^|;&]*\|\s*(ba)?sh/i, "curl | sh"],
	[/\beval\b/i, "eval"],
	[/\bnc\b[^|;&]*\s-l/i, "netcat listener"],
];

// What a container must never be allowed to do. A rootless container is
// already confined to the user's own files; these rules stop it reaching
// further - the host namespaces, the runtime socket, or anything outside the
// workspace.
//
// The rules are owned by sadoc.policy and handed over in the environment, so
// there is one list of them rather than one here and one there.
const CONTAINER_FORBIDDEN: Array<[RegExp, string]> = readPolicy().container;
const RUNTIME_PATTERNS: Array<[string, RegExp, boolean]> = readPolicy().runtimes;

interface Policy {
	container: Array<[RegExp, string]>;
	runtimes: Array<[string, RegExp, boolean]>;
}

/**
 * The rules are owned by sadoc.policy and handed over in the environment, so
 * there is one list of them rather than one here and one there.
 */
function readPolicy(): Policy {
	const empty: Policy = { container: [], runtimes: [] };
	const raw = process.env.SADOC_CONTAINER_POLICY;
	if (!raw) {
		return empty;
	}
	try {
		const parsed = JSON.parse(raw);
		const container = Array.isArray(parsed?.container)
			? parsed.container
					.filter((rule) => Array.isArray(rule) && typeof rule[0] === "string")
					.map((rule) => [new RegExp(rule[0], "i"), String(rule[1] ?? "that")])
			: [];
		const runtimes = Array.isArray(parsed?.runtimes)
			? parsed.runtimes
					.filter((rule) => Array.isArray(rule) && typeof rule[0] === "string")
					.map((rule) => [
						String(rule[0]),
						new RegExp(rule[1], "i"),
						rule[2] !== false,
					])
			: [];
		return { container, runtimes };
	} catch {
		return empty;
	}
}

function q(s: string): string {
	return "'" + s.replace(/'/g, `'\\''`) + "'";
}

/**
 * A mount whose source is outside the workspace and the cache.
 *
 * A container may have the repository and the tool cache, and nothing else.
 * A source that is relative, or that the shell expands, is inside the sandbox
 * already; a source that names a host directory is not.
 */
function mountSourceViolation(command: string): string | null {
	const roots = [WORKSPACE, TOOL_CACHE];
	const patterns = [
		/(?:-v|--volume)[= ]+("[^"]*"|'[^']*'|[^\s:]+)/g,
		/--mount[= ][^\n]*?\bsource=("[^"]*"|'[^']*'|[^\s,]+)/g,
	];
	for (const pattern of patterns) {
		for (const match of command.matchAll(pattern)) {
			let source = (match[1] ?? "").replace(/^["']|["']$/g, "");
			if (!source) {
				continue;
			}
			// Expanded by the shell inside the sandbox: the scratch home, the
			// working directory, a variable the recipe sets itself.
			if (source.startsWith("$") || source.startsWith("~")) {
				continue;
			}
			// Relative to the working directory, which is the workspace.
			if (!source.startsWith("/")) {
				continue;
			}
			if (roots.some((root) => source === root || source.startsWith(root + "/"))) {
				continue;
			}
			return "a directory outside the workspace";
		}
	}
	return null;
}

/**
 * The reason a command is not allowed, or null if it is fine.
 *
 * `allowedRuntime` is the one container runtime that may be used, if any.
 * Every other runtime is refused like any other command, and Docker always
 * is: its socket is root-equivalent, so reaching the daemon would be
 * reaching root.
 *
 * The runtime that is allowed is still bound by the container rules: no host
 * namespaces, no runtime socket, no host directories, no extra capabilities.
 */
export function policyReason(command: string, allowedRuntime: string | null): string | null {
	for (const [pattern, label] of FORBIDDEN) {
		if (pattern.test(command)) {
			return label;
		}
	}
	for (const [name, pattern, mayEnable] of RUNTIME_PATTERNS) {
		if (mayEnable && allowedRuntime && name === allowedRuntime) {
			continue;
		}
		if (pattern.test(command)) {
			return name;
		}
	}
	if (allowedRuntime) {
		for (const [pattern, label] of CONTAINER_FORBIDDEN) {
			if (pattern.test(command)) {
				return label;
			}
		}
		const outside = mountSourceViolation(command);
		if (outside) {
			return outside;
		}
	}
	return null;
}

/** Build the bubblewrap argv for a single command. */
export function buildSandbox(command: string, cwd: string): string {
	const scratch = "/tmp/depsbot-scratch";
	const cache = TOOL_CACHE;
	const realHome = process.env.SADOC_REAL_HOME || "/root";
	// NOTE: /bin/sh is bash in POSIX mode on this box, where sourcing a missing
	// file aborts the whole script. Use bash explicitly and guard every source.
	const inner = [
		"set +e",
		"[ -r /etc/profile ] && . /etc/profile",
		`[ -r ${q(realHome)}/.profile ] && . ${q(realHome)}/.profile`,
		`export HOME=${q(scratch)}`,
		`export XDG_CACHE_HOME=${q(scratch)}/.cache`,
		// The persistent cache, named so the agent can install tools into it
		// instead of into a temporary directory that is thrown away each run.
		`export SADOC_TOOL_CACHE=${q(cache)}`,
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
		// A rootless runtime keeps its images and containers in the cache
		// directory, not in the real home.
		...(ALLOW_CONTAINERS
			? [
				`export XDG_DATA_HOME=${q(cache)}/containers/data`,
				`export XDG_RUNTIME_DIR=${q(cache)}/containers/run`,
				// The runtime the agent should use, so it does not have to guess.
				`export SADOC_CONTAINER_RUNTIME=${q(RUNTIME)}`,
				`mkdir -p ${q(cache)}/containers/data ${q(cache)}/containers/run`,
				`chmod 700 ${q(cache)}/containers/run`,
			]
			: []),
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
		"--setenv SADOC_SANDBOX 1",
		// NOTE: the inner script must be quoted, otherwise the *outer* shell that
		// pi spawns splits it on ";" / "||" and runs parts outside the sandbox.
		"bash", "-c", q(inner),
	];
	return argv.join(" ");
}

export default function (pi: ExtensionAPI) {
	// RPC `bash` is a user command, not a model tool call. It does not pass
	// through tool_call, but runs the same discovered, untrusted build recipe.
	pi.on("user_bash", (event: any) => {
		const command = String(event.command ?? "");
		const label = policyReason(command, ALLOW_CONTAINERS ? RUNTIME : null);
		if (label) {
			return { result: {
				output: `Blocked by sadoc policy: ${label}`,
				exitCode: 1, cancelled: false, truncated: false,
			} };
		}
		const local = createLocalBashOperations();
		return { operations: {
			exec(command: string, cwd: string, options: any) {
				return local.exec(buildSandbox(command, cwd), cwd, options);
			},
		} };
	});

	pi.on("tool_call", async (event: any) => {
		if (SHELL_TOOLS.has(event.toolName)) {
			const command = String(event.input.command ?? "");
			// The runtime is allowed here too when it has been enabled: the model
			// has to be able to build and run a container while it works out how
			// the project builds, or it can never discover such a recipe.
			const label = policyReason(command, ALLOW_CONTAINERS ? RUNTIME : null);
			if (label) {
				return {
					block: true,
					reason:
						`Blocked by sadoc policy: "${label}" is not allowed in the agent sandbox. ` +
						`The orchestrator owns all remote state (push, PR create, PR merge).`,
				};
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
