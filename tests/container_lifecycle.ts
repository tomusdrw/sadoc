import sandbox, { buildSandbox } from './sandbox.ts';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const handlers: Record<string, Function> = {};
sandbox({on(name: string, handler: Function) { handlers[name] = handler; },
    getActiveTools() { return ["bash", "read", "write", "edit"]; },
    setActiveTools() {},
} as any);
async function run(command: string, seconds = 10) {
    return await new Promise<{code: number, output: string}>(resolve => {
        const proc = spawn('bash', ['-c', buildSandbox(command, process.cwd(), seconds)]);
        let output = '';
        proc.stdout.on('data', chunk => output += chunk);
        proc.stderr.on('data', chunk => output += chunk);
        proc.on('close', code => resolve({code: code ?? 1, output}));
    });
}
const first = await run('podman run --rm example.invalid/image echo hello');
assert.equal(first.code, 0, first.output);
const second = await run('podman container create example.invalid/image');
assert.equal(second.code, 0, second.output);
// The shim receives expanded argv, including spaces and literal quotes in paths.
for (const mount of ['--volume "$SADOC_WORKSPACE:/w:rw"', '-v"$SADOC_WORKSPACE:/w"',
    '--volume="$SADOC_WORKSPACE:/w"', '--mount "type=bind,source=$SADOC_WORKSPACE,target=/w"']) {
    const allowed = await run(`podman run ${mount} example.invalid/image`);
    assert.equal(allowed.code, 0, allowed.output);
}
for (const mount of ['--volume "$SADOC_WORKSPACE other:/w"',
    '--mount "type=bind,src=$SADOC_WORKSPACE other,target=/w"']) {
    const outside = await run(`podman run ${mount} example.invalid/image`);
    assert.notEqual(outside.code, 0);
    assert.ok(outside.output.includes('Blocked by sadoc policy'), outside.output);
}
const global = await run('podman --log-level=error run example.invalid/image');
assert.notEqual(global.code, 0);
assert.ok(global.output.includes('Blocked by sadoc policy'), global.output);
const nestedGlobal = await run('podman container --log-level=error run example.invalid/image');
assert.notEqual(nestedGlobal.code, 0);
const calls = readFileSync(process.env.FAKE_RUNTIME_LOG!, 'utf8').trim().split('\n').map(line => JSON.parse(line));
assert.equal(calls.length, 6);
assert.ok(calls[2].includes(`${process.env.SADOC_WORKSPACE}:/w:rw`));
for (const call of calls) assert.ok(call.includes(`io.sadoc.session=${process.env.SADOC_SESSION_ID}`));
// Even a direct shim request must be refused by the helper during assessment.
await handlers.before_agent_start({messageType: "user", prompt: "[SADOC_READ_ONLY]\nAssess"});
const blocked = await run('podman run -v "$SADOC_WORKSPACE:/w" example.invalid/image touch /w/unexpected');
assert.notEqual(blocked.code, 0, blocked.output);
assert.match(blocked.output, /read-only assessment/);
assert.equal(readFileSync(process.env.FAKE_RUNTIME_LOG!, 'utf8').trim().split('\n').length, 6);
await handlers.before_agent_start({messageType: "user", prompt: "Apply"});
// A helper command also terminates independently of the sandbox process.
const timeout = await run('podman wait forever', 0.2);
assert.equal(timeout.code, 124, timeout.output);
const sockets = await run('printf "%s" "$SADOC_CONTAINER_SOCKET"');
assert.ok(sockets.output.endsWith('/test-session.sock'), sockets.output);
console.log('lifecycle checks pass');
process.exit(0);
