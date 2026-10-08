// The api Worker: forwards every request to the homelab API and tells it who the
// client is. The backend trusts X-Zet-Client-IP only next to the matching
// X-Zet-Proxy-Key (TRUSTED_PROXY_KEY on the server, PROXY_KEY here).
//
// Bindings: ORIGIN (plain text, e.g. https://api-origin.example) and
// PROXY_KEY (secret).

const CLIENT_IP_HEADER = 'X-Zet-Client-IP';
const PROXY_KEY_HEADER = 'X-Zet-Proxy-Key';

export default {
	async fetch(request, env) {
		const url = new URL(request.url);
		const target = new URL(url.pathname + url.search, env.ORIGIN);
		const forwarded = new Request(target, request);

		// A client must never be able to pick its own address.
		forwarded.headers.delete(CLIENT_IP_HEADER);
		forwarded.headers.delete(PROXY_KEY_HEADER);

		const clientIp = request.headers.get('CF-Connecting-IP');
		if (clientIp && env.PROXY_KEY) {
			forwarded.headers.set(CLIENT_IP_HEADER, clientIp);
			forwarded.headers.set(PROXY_KEY_HEADER, env.PROXY_KEY);
		}
		return fetch(forwarded);
	}
};
