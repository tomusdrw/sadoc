import { buildSandbox, startContainerHelper } from './sandbox.ts';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

startContainerHelper();
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
const global = await run('podman --log-level=error run example.invalid/image');
assert.notEqual(global.code, 0);
assert.ok(global.output.includes('Blocked by sadoc policy'), global.output);
const nestedGlobal = await run('podman container --log-level=error run example.invalid/image');
assert.notEqual(nestedGlobal.code, 0);
const calls = readFileSync(process.env.FAKE_RUNTIME_LOG!, 'utf8').trim().split('\n').map(line => JSON.parse(line));
for (const call of calls) assert.ok(call.includes(`io.sadoc.session=${process.env.SADOC_SESSION_ID}`));
// A helper command also terminates independently of the sandbox process.
const timeout = await run('podman wait forever', 0.2);
assert.equal(timeout.code, 124, timeout.output);
const sockets = await run('printf "%s" "$SADOC_CONTAINER_SOCKET"');
assert.ok(sockets.output.endsWith('/test-session.sock'), sockets.output);
console.log('lifecycle checks pass');
process.exit(0);
