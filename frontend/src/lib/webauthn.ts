// Passkeys: the server sends WebAuthn options as JSON (binary fields base64url-encoded) and expects the browser's
// credential back in the same shape. Modern browsers parse/serialize natively; older ones get the manual conversion.

type Json = Record<string, unknown>;

const toBuf = (s: string): ArrayBuffer => {
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
};

const toB64u = (b: ArrayBuffer | null | undefined): string | null => {
  if (!b) return null;
  const bytes = new Uint8Array(b);
  let s = '';
  for (const x of bytes) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};

export function passkeysSupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential && !!navigator.credentials;
}

type PKC = typeof PublicKeyCredential & {
  parseCreationOptionsFromJSON?: (o: Json) => PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON?: (o: Json) => PublicKeyCredentialRequestOptions;
};

function creationOptions(o: Json): PublicKeyCredentialCreationOptions {
  const P = window.PublicKeyCredential as PKC;
  if (P.parseCreationOptionsFromJSON) return P.parseCreationOptionsFromJSON(o);
  const user = o.user as Json;
  return {
    ...(o as unknown as PublicKeyCredentialCreationOptions),
    challenge: toBuf(o.challenge as string),
    user: { ...(user as unknown as PublicKeyCredentialUserEntity), id: toBuf(user.id as string) },
    excludeCredentials: ((o.excludeCredentials as Json[]) ?? []).map((c) => ({ ...(c as object), id: toBuf(c.id as string) }) as PublicKeyCredentialDescriptor),
  };
}

function requestOptions(o: Json): PublicKeyCredentialRequestOptions {
  const P = window.PublicKeyCredential as PKC;
  if (P.parseRequestOptionsFromJSON) return P.parseRequestOptionsFromJSON(o);
  return {
    ...(o as unknown as PublicKeyCredentialRequestOptions),
    challenge: toBuf(o.challenge as string),
    allowCredentials: ((o.allowCredentials as Json[]) ?? []).map((c) => ({ ...(c as object), id: toBuf(c.id as string) }) as PublicKeyCredentialDescriptor),
  };
}

function serialize(cred: PublicKeyCredential): Json {
  const withJson = cred as unknown as { toJSON?: () => unknown };
  if (typeof withJson.toJSON === 'function') return withJson.toJSON() as Json;
  const r = cred.response as AuthenticatorAttestationResponse & AuthenticatorAssertionResponse;
  const response: Json = { clientDataJSON: toB64u(r.clientDataJSON) };
  if ('attestationObject' in r && r.attestationObject) response.attestationObject = toB64u(r.attestationObject);
  if ('authenticatorData' in r && r.authenticatorData) {
    response.authenticatorData = toB64u(r.authenticatorData);
    response.signature = toB64u(r.signature);
    response.userHandle = toB64u(r.userHandle);
  }
  return { id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, response };
}

/** Register a new passkey with options from /api/auth/mfa/webauthn/begin. */
export async function createPasskey(options: Json): Promise<Json> {
  const cred = (await navigator.credentials.create({ publicKey: creationOptions(options) })) as PublicKeyCredential | null;
  if (!cred) throw new Error('No passkey was created.');
  return serialize(cred);
}

/** Use a passkey to answer a sign-in challenge (options from /api/auth/login). */
export async function getPasskeyAssertion(options: Json): Promise<Json> {
  const cred = (await navigator.credentials.get({ publicKey: requestOptions(options) })) as PublicKeyCredential | null;
  if (!cred) throw new Error('No passkey was used.');
  return serialize(cred);
}
