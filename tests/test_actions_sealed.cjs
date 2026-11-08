const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {test} = require('node:test');
const {open, sealResult} = require('../deploy/actions/sealed.cjs');

test('binds request and result to the request ID and rejects expiry and reruns', () => {
  const {privateKey, publicKey} = crypto.generateKeyPairSync('rsa', {modulusLength: 2048});
  const request = 'a'.repeat(32);
  const resultKey = crypto.randomBytes(32);
  function envelope(expires) {
    const key = crypto.randomBytes(32), nonce = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
    cipher.setAAD(Buffer.from('inss-credentials:v1:' + request));
    const payload = JSON.stringify({request, expires, cpf: '00000000000', password: 'fixture',
      result_key: resultKey.toString('base64url')});
    const body = Buffer.concat([cipher.update(payload), cipher.final()]);
    const wrapped = crypto.publicEncrypt({key: publicKey, oaepHash: 'sha256'}, key);
    return ['v1', ...[wrapped, nonce, cipher.getAuthTag(), body].map(x => x.toString('base64url'))].join('.');
  }
  const now = Math.floor(Date.now() / 1000);
  const input = {sealed: envelope(now + 60), private_key: privateKey, request, attempt: '1'};
  assert.equal(open(input).GOVBR_PASSWORD, 'fixture');
  assert.throws(() => open({...input, request: 'b'.repeat(32)}));
  assert.throws(() => open({...input, attempt: '2'}));
  assert.throws(() => open({...input, sealed: envelope(now - 60)}));

  const [, iv, tag, body] = sealResult({key: open(input).result_key, request, payload: '{"ok":true}'}).split('.');
  function decode(id) {
    const decipher = crypto.createDecipheriv('aes-256-gcm', resultKey, Buffer.from(iv, 'base64url'));
    decipher.setAAD(Buffer.from('inss-result:v1:' + id));
    decipher.setAuthTag(Buffer.from(tag, 'base64url'));
    return Buffer.concat([decipher.update(Buffer.from(body, 'base64url')), decipher.final()]).toString();
  }
  assert.equal(decode(request), '{"ok":true}');
  assert.throws(() => decode('b'.repeat(32)));
});
