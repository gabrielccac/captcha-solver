'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');

function bytes(value, length) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('invalid_encoding');
  const decoded = Buffer.from(value, 'base64url');
  if (decoded.toString('base64url') !== value || (length && decoded.length !== length)) throw new Error('invalid_size');
  return decoded;
}

function open({sealed, private_key, request, attempt}) {
  if (!/^[a-f0-9]{32}$/.test(request) || attempt !== '1' || typeof sealed !== 'string' || sealed.length > 4096) {
    throw new Error('invalid_request');
  }
  const [version, wrapped, nonce, tag, body, extra] = sealed.split('.');
  if (version !== 'v1' || extra !== undefined) throw new Error('invalid_envelope');
  const key = crypto.privateDecrypt({key: private_key, oaepHash: 'sha256',
    padding: crypto.constants.RSA_PKCS1_OAEP_PADDING}, bytes(wrapped));
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, bytes(nonce, 12));
  decipher.setAAD(Buffer.from('inss-credentials:v1:' + request));
  decipher.setAuthTag(bytes(tag, 16));
  const value = JSON.parse(Buffer.concat([decipher.update(bytes(body)), decipher.final()]).toString('utf8'));
  const now = Math.floor(Date.now() / 1000);
  if (value.request !== request || !Number.isInteger(value.expires) || value.expires < now ||
      value.expires > now + 600 || typeof value.cpf !== 'string' || !/^[0-9]{11}$/.test(value.cpf) ||
      typeof value.password !== 'string' || !value.password.trim() || value.password.length > 256) {
    throw new Error('invalid_credentials_envelope');
  }
  bytes(value.result_key, 32);
  const claim = {GOVBR_CPF: value.cpf, GOVBR_PASSWORD: value.password, result_key: value.result_key};
  if (value.callback_url !== undefined) claim.callback_url = callbackUrl(value.callback_url);
  if (value.options !== undefined) claim.options = queryOptions(value.options);
  return claim;
}

// The result is POSTed here; it travels inside the envelope so it never shows in public run inputs.
function callbackUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('invalid_callback_url'); }
  if (typeof value !== 'string' || value.length > 2048 || url.protocol !== 'https:' || url.username || url.password) {
    throw new Error('invalid_callback_url');
  }
  return url.href;
}

// Present options mean "log in and run the queries in the runner" instead of exporting API tokens.
function queryOptions(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_options');
  const years = value.years === undefined ? 5 : value.years;
  const protocol = value.protocol === undefined ? '' : value.protocol;
  if (!Number.isInteger(years) || years < 1 || years > 5 || typeof protocol !== 'string' ||
      (protocol && !/^[0-9]{8,16}$/.test(protocol)) ||
      Object.keys(value).some(key => !['years', 'protocol', 'medical_reports'].includes(key))) {
    throw new Error('invalid_options');
  }
  return {years, protocol, medical_reports: value.medical_reports === true};
}

function sealResult({key, request, payload}) {
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', bytes(key, 32), nonce);
  cipher.setAAD(Buffer.from('inss-result:v1:' + request));
  const body = Buffer.concat([cipher.update(payload, 'utf8'), cipher.final()]);
  return ['v1', nonce.toString('base64url'), cipher.getAuthTag().toString('base64url'), body.toString('base64url')].join('.');
}

if (require.main === module) {
  try {
    const value = JSON.parse(fs.readFileSync(0, 'utf8'));
    if (process.argv[2] === 'open') process.stdout.write(JSON.stringify(open(value)));
    else if (process.argv[2] === 'seal-result') process.stdout.write(sealResult(value));
    else process.exitCode = 1;
  } catch {
    process.exitCode = 1;
  }
}

module.exports = {open, sealResult};
