// Run with: node --test deploy/cloudflare/worker.test.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import worker from './worker.js';

const env = { ORIGIN: 'https://origin.example', PROXY_KEY: 'secret-key' };

async function forward(init, bindings = env) {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return new Response('ok');
	};
	await worker.fetch(new Request('https://api.zetchat.app/auth/login?x=1', init), bindings);
	return seen;
}

test('forwards to the origin with the path and query', async () => {
	const seen = await forward({ method: 'POST', body: '{"a":1}' });
	assert.equal(seen.url, 'https://origin.example/auth/login?x=1');
	assert.equal(seen.method, 'POST');
	assert.equal(await seen.text(), '{"a":1}');
});

test('adds the client address and the key', async () => {
	const seen = await forward({ headers: { 'CF-Connecting-IP': '198.51.100.7' } });
	assert.equal(seen.headers.get('X-Zet-Client-IP'), '198.51.100.7');
	assert.equal(seen.headers.get('X-Zet-Proxy-Key'), 'secret-key');
});

test('drops the headers a client sent itself', async () => {
	const seen = await forward({
		headers: {
			'CF-Connecting-IP': '198.51.100.7',
			'X-Zet-Client-IP': '10.0.0.1',
			'X-Zet-Proxy-Key': 'guess'
		}
	});
	assert.equal(seen.headers.get('X-Zet-Client-IP'), '198.51.100.7');
	assert.equal(seen.headers.get('X-Zet-Proxy-Key'), 'secret-key');
});

test('sends neither header without a key binding', async () => {
	const seen = await forward(
		{ headers: { 'CF-Connecting-IP': '198.51.100.7', 'X-Zet-Client-IP': '10.0.0.1' } },
		{ ORIGIN: env.ORIGIN }
	);
	assert.equal(seen.headers.get('X-Zet-Client-IP'), null);
	assert.equal(seen.headers.get('X-Zet-Proxy-Key'), null);
});

test('keeps the other headers, including a websocket upgrade', async () => {
	const seen = await forward({
		headers: { Upgrade: 'websocket', Cookie: 'access_token=abc', Origin: 'https://zetchat.app' }
	});
	assert.equal(seen.headers.get('Upgrade'), 'websocket');
	assert.equal(seen.headers.get('Cookie'), 'access_token=abc');
	assert.equal(seen.headers.get('Origin'), 'https://zetchat.app');
});
